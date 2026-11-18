"""Bridge the album workbench to the private catalog and capture socket."""
from __future__ import annotations

import json
from pathlib import Path
import re
import socket
from threading import Lock
import uuid

from catalog import Catalog, Conflict, InvalidMaster, validate_wav
from release_catalog import ReleaseCatalog, RevisionConflict


def _forward(path, packet):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(10)
        connection.connect(str(path))
        connection.sendall(packet + b"\n")
        return connection.makefile().readline(64_000).encode()


class AlbumService:
    def __init__(self, catalog: Catalog, control_socket: str | Path, *,
                 releases: ReleaseCatalog | None = None, send=_forward):
        self.catalog = catalog
        self.control_socket = Path(control_socket)
        self.releases = releases or ReleaseCatalog(catalog.path)
        self.send = send
        self._recording_lock = Lock()

    def bootstrap(self, project_id=None):
        try:
            capture = self._control({"action": "status"})
        except Exception as error:
            capture = {"recording": "unavailable", "error": str(error)}
        result = {"schema_version": 1, "projects": self.catalog.projects(),
                  "history": self.catalog.history(limit=100), "capture": capture}
        if isinstance(project_id, str) and self.catalog.get_project(project_id):
            result["side_a_boundary"] = self.catalog.get_side_boundary(
                project_id, capture.get("capture_generation"))
        else:
            result["side_a_boundary"] = None
        return result

    def create_project(self, data):
        label = data.get("label", "")
        if not isinstance(label, str) or len(label) > 300 or "\0" in label:
            raise ValueError("Album name must be text of no more than 300 characters")
        return self.catalog.create_project(label.strip())

    def edit_project(self, project_id, data):
        if not re.fullmatch(r"[0-9a-f-]{36}", project_id, re.I):
            raise ValueError("Invalid album ID")
        revision = data.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("Reload the album before saving your changes")
        label = data.get("label")
        if not isinstance(label, str) or len(label) > 300 or "\0" in label:
            raise ValueError("Album name must be text of no more than 300 characters")
        return self.catalog.edit_project(project_id, revision, label=label.strip())

    def search_releases(self, data):
        allowed = {key: data.get(key) for key in
                   ("album", "artist", "catalog_number", "barcode", "recording_isrc", "title")}
        return {"candidates": self.releases.search(**allowed)}

    def preview_release(self, data):
        release_mbid = data.get("release_mbid")
        if not isinstance(release_mbid, str):
            raise ValueError("Choose a release candidate to preview")
        return self.releases.fetch_release(release_mbid)

    def artwork_candidates(self, data):
        release_mbid = data.get("release_mbid")
        if not isinstance(release_mbid, str):
            raise ValueError("Choose a release candidate first")
        return {"candidates": self.releases.cover_art_candidates(release_mbid)}

    def artwork_thumbnail(self, release_mbid, image_id):
        return self.releases.cover_thumbnail(release_mbid, image_id)

    def pin_release(self, data):
        project_id, mbid, revision = data.get("project_id"), data.get("release_mbid"), data.get("revision")
        if not isinstance(project_id, str) or self.catalog.get_project(project_id) is None:
            raise ValueError("Choose an existing album first")
        if type(revision) is not int or revision < 0:
            raise ValueError("Reload release choices before applying one")
        cover_image_id = data.get("cover_image_id")
        if cover_image_id is not None and not isinstance(cover_image_id, (str, int)):
            raise ValueError("Choose a cover from this release")
        return self.releases.pin(project_id, mbid, expected_revision=revision,
                                 cover_image_id=cover_image_id)

    def start_take(self, data):
        project_id, side = data.get("project_id"), data.get("side")
        if not isinstance(project_id, str) or self.catalog.get_project(project_id) is None:
            raise ValueError("Choose an album before recording")
        if side not in ("A", "B"):
            raise ValueError("Choose Side A or Side B")
        if type(data.get("buffered", False)) is not bool:
            raise ValueError("Buffer choice must be true or false")
        limit = data.get("limit_seconds", 1800)
        if type(limit) is not int or not 1 <= limit <= 3600:
            raise ValueError("Recording length must be between 1 and 3600 seconds")
        request_id = data.get("request_id") or str(uuid.uuid4())
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Invalid save request ID") from exc
        with self._recording_lock:
            existing = self.catalog.get_intent(request_id)
            if existing:
                if existing["project_id"] != project_id or existing["side"] != side:
                    raise Conflict("Save request ID was used for a different side")
                if existing["state"] == "committed":
                    return json.loads(existing["result"])
                if existing["state"] in ("writing", "closed", "verified"):
                    return {"request_id": request_id, "take_id": existing["take_id"],
                            "state": existing["state"], "side": side}
                raise Conflict("This save request already ended; start a new side")
            project = self.catalog.get_project(project_id)
            if type(data.get("revision")) is not int or data["revision"] != project["revision"]:
                raise Conflict("Album changed; reload before starting a side")
            status = self._control({"action": "status"})
            generation = status.get("capture_generation")
            boot = status.get("boot_generation")
            if not isinstance(generation, str) or not isinstance(boot, str):
                raise RuntimeError("Capture session does not expose a stable generation")
            overlap_ack = data.get("overlap_ack", False)
            if type(overlap_ack) is not bool:
                raise ValueError("Overlap confirmation must be true or false")
            unknown_boundary_ack = data.get("unknown_boundary_ack", False)
            if type(unknown_boundary_ack) is not bool:
                raise ValueError("Unknown-boundary confirmation must be true or false")
            capture_command = {"action": "save_buffer" if data.get("buffered", False) else "record",
                               "limit": limit}
            if side == "B" and data.get("buffered", False):
                boundary = self.catalog.get_side_boundary(project_id, generation)
                if (boundary is None and status.get("side_a_end_capture_generation") == generation
                        and type(status.get("side_a_end_frame")) is int):
                    boundary = {"end_frame": status["side_a_end_frame"]}
                if boundary is None and not unknown_boundary_ack:
                    raise ValueError("No saved Side A boundary is known. Confirm the possible overlap before saving this buffer.")
                oldest, newest = status.get("buffered_oldest_frame"), status.get("buffered_newest_frame")
                if (type(oldest) is not int or type(newest) is not int
                        or oldest < 0 or newest < oldest):
                    raise RuntimeError("Capture session has no valid retained range")
                start_frame = data.get("buffered_start_frame")
                if start_frame is None and boundary is not None:
                    start_frame = boundary["end_frame"]
                if start_frame is not None:
                    if type(start_frame) is not int or not oldest <= start_frame < newest:
                        raise ValueError("The selected Side B start expired; choose a fresh buffer interval.")
                    if boundary and start_frame < boundary["end_frame"] and not overlap_ack:
                        raise ValueError("Confirm that Side B should include audio before the saved Side A end.")
                    capture_command.update({"buffered_start_frame": start_frame,
                                            "expected_capture_generation": generation,
                                            "side_a_boundary_frame": boundary["end_frame"] if boundary else None,
                                            "overlap_ack": overlap_ack})
            staging_id = str(uuid.uuid4())
            stage_name = f".rr-{staging_id}.staging.wav"
            published_name = f"rr-{staging_id}-side-{side}.wav"
            intent = self.catalog.begin_take(
                request_id=request_id, project_id=project_id, side=side,
                capture_generation=generation, boot_generation=boot,
                staging_locator=stage_name,
                intended_locator=published_name,
                expected_revision=data["revision"],
            )
            capture_command.update({"name": stage_name, "request_id": request_id, "side": side})
            try:
                result = self._control(capture_command)
                if "error" in result:
                    raise RuntimeError(result["error"])
            except Exception as exc:
                try:
                    observed = self._control({"action": "status"})
                except Exception as status_error:
                    # The command may have reached the session even though its
                    # response was lost. Keep its intent for status/restart recovery.
                    raise RuntimeError("Start result is uncertain; check capture status before retrying") from status_error
                if observed.get("recording_id") == request_id:
                    return {"request_id": request_id, "take_id": intent["take_id"],
                            "state": "recording", "side": side}
                self.catalog.fail_take(request_id, str(exc))
                raise
            return {"request_id": request_id, "take_id": intent["take_id"],
                    "state": "recording", "side": side}

    def stop_take(self, data):
        request_id = data.get("request_id")
        if not isinstance(request_id, str):
            raise ValueError("Specify the active side to stop")
        with self._recording_lock:
            intent = self.catalog.get_intent(request_id)
            if not intent:
                raise ValueError("Recording intent was not found")
            if intent["state"] == "committed":
                return json.loads(intent["result"])
            if intent["state"] not in ("writing", "closed", "verified"):
                raise ValueError("This side is no longer recording")
            result = {}
            if intent["state"] == "writing":
                try:
                    result = self._control({"action": "stop", "request_id": request_id})
                except Exception as stop_error:
                    observed = self._observe_status(request_id)
                    if observed is None or observed.get("recording_id") == request_id:
                        raise RuntimeError("Stop result is uncertain; the take remains recoverable") from stop_error
                    return self._recover_or_interrupt(intent, "Stop result was lost")
                if "error" in result:
                    observed = self._observe_status(request_id)
                    if observed is None:
                        raise RuntimeError("Stop result is uncertain; the take remains recoverable")
                    if observed.get("recording_id") == request_id:
                        raise RuntimeError("Recorder is still finalizing; retry Stop to collect its receipt")
                    return self._recover_or_interrupt(intent, result["error"])
                frames = result.get("recorded_frames")
                if type(frames) is not int or frames <= 0:
                    current = self.catalog.get_intent(request_id)
                    if current and current["state"] in ("closed", "verified"):
                        return self._publish_closed(current, {})
                    observed = self._observe_status(request_id)
                    if observed is None:
                        raise RuntimeError("Stop result is uncertain; the take remains recoverable")
                    if observed.get("recording_id") == request_id:
                        raise RuntimeError("Recorder is still finalizing; retry Stop to collect its receipt")
                    return self._recover_or_interrupt(intent, "Recorder returned no complete frames")
                boundary = result.get("side_a_end_frame") if intent["side"] == "A" else None
                boundary_generation = result.get("side_a_end_capture_generation")
                if boundary_generation != intent["capture_generation"]:
                    boundary = None
                self.catalog.close_take(request_id, expected_frames=frames,
                                        side_a_end_frame=boundary)
                intent = self.catalog.get_intent(request_id)
            try:
                return self._publish_closed(intent, result if intent["state"] == "closed" else {})
            except (InvalidMaster, Conflict) as exc:
                self.catalog.quarantine_take(request_id, str(exc))
                raise

    def _publish_closed(self, intent, result):
        if intent["side"] == "A" and intent.get("side_a_end_frame") is None:
            end_frame = result.get("side_a_end_frame")
            generation = result.get("side_a_end_capture_generation")
            if (type(end_frame) is not int or generation != intent["capture_generation"]):
                observed = self._observe_status(intent["request_id"])
                if observed and observed.get("side_a_end_capture_generation") == intent["capture_generation"]:
                    end_frame = observed.get("side_a_end_frame")
                    generation = observed.get("side_a_end_capture_generation")
            if type(end_frame) is int and generation == intent["capture_generation"]:
                intent = self.catalog.close_take(intent["request_id"],
                                                 expected_frames=intent["expected_frames"],
                                                 side_a_end_frame=end_frame)
        return self.catalog.publish_take(intent["request_id"],
                                         expected_frames=intent["expected_frames"])

    def _observe_status(self, request_id):
        try:
            status = self._control({"action": "status"})
            return status if "error" not in status else None
        except Exception:
            return None

    def _recover_or_interrupt(self, intent, reason):
        request_id = intent["request_id"]
        stage = self.catalog._relative(intent["staging_locator"])
        if stage.is_symlink():
            self.catalog.quarantine_take(request_id, "Staging WAV is a symbolic link")
            raise InvalidMaster("Staging WAV is a symbolic link")
        if stage.exists():
            try:
                info = validate_wav(stage, intent.get("expected_frames"))
                boundary = None
                if intent["side"] == "A":
                    observed = self._observe_status(request_id)
                    if observed and observed.get("side_a_end_capture_generation") == intent["capture_generation"]:
                        boundary = observed.get("side_a_end_frame")
                if intent["state"] == "writing":
                    intent = self.catalog.close_take(request_id, expected_frames=info["frames"],
                                                     side_a_end_frame=boundary)
                elif boundary is not None:
                    intent = self.catalog.close_take(request_id, expected_frames=info["frames"],
                                                     side_a_end_frame=boundary)
                return self._publish_closed(intent, {})
            except (InvalidMaster, Conflict) as error:
                self.catalog.quarantine_take(request_id, str(error))
                raise
        self.catalog.fail_take(request_id, reason)
        raise RuntimeError(reason)

    def _control(self, message):
        raw = self.send(self.control_socket, json.dumps(message).encode())
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise RuntimeError("Capture session returned an invalid response")
        return result

