import json
from pathlib import Path
import tempfile
import wave
import unittest
import io

from album_service import AlbumService
from catalog import Catalog, Conflict, InvalidMaster
from session import Session


class AlbumServiceTests(unittest.TestCase):
    def test_side_b_without_saved_boundary_requires_explicit_acknowledgment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            library.mkdir()
            catalog = Catalog(root / "state" / "catalog.sqlite", library)
            project = catalog.create_project("Fixture")
            saved = []

            def control(_path, packet):
                command = json.loads(packet)
                if command["action"] == "status":
                    return json.dumps({"capture_generation": "g", "boot_generation": "b",
                                       "buffered_oldest_frame": 0,
                                       "buffered_newest_frame": 48000}).encode()
                saved.append(command)
                return b"{}"

            service = AlbumService(catalog, root / "control.sock", send=control)
            request = {"project_id": project["id"], "revision": 1, "side": "B", "buffered": True,
                       "buffered_start_frame": 0}
            with self.assertRaisesRegex(ValueError, "No saved Side A boundary"):
                service.start_take(request)
            self.assertFalse(saved)
            request["unknown_boundary_ack"] = True
            started = service.start_take(request)
            self.assertEqual(started["state"], "recording")
            self.assertEqual(saved[0]["action"], "save_buffer")

    def test_take_is_written_to_staging_then_published_with_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            library.mkdir()
            catalog = Catalog(root / "state" / "catalog.sqlite", library)
            project = catalog.create_project("Fixture album")
            sent = []

            def control(_path, packet):
                command = json.loads(packet)
                sent.append(command)
                if command["action"] == "status":
                    return json.dumps({"capture_generation": "capture-7", "boot_generation": "boot-3"}).encode()
                if command["action"] == "record":
                    stage = library / command["name"]
                    with wave.open(str(stage), "wb") as writer:
                        writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                        writer.writeframes(b"\x01\x00\x02\x00" * 4)
                    stage.with_suffix(".matches.json").write_text(
                        '[{"frame":0,"tags":{"artist":"Fixture Artist","title":"Fixture Song"}}]')
                    return b"{}"
                if command["action"] == "stop":
                    return b'{"recorded_frames":4}'
                raise AssertionError(command)

            service = AlbumService(catalog, root / "control.sock", send=control)
            active = service.start_take({"project_id": project["id"], "revision": 1, "side": "B"})
            intent = catalog.get_intent(active["request_id"])
            staging = library / intent["staging_locator"]
            destination = library / intent["intended_locator"]
            self.assertTrue(staging.is_file())
            self.assertFalse(destination.exists())
            receipt = service.stop_take({"request_id": active["request_id"]})
            self.assertEqual(receipt["frames"], 4)
            self.assertEqual(receipt["side"], "B")
            self.assertTrue(destination.is_file())
            self.assertFalse(staging.exists())
            self.assertTrue(destination.with_suffix(".matches.json").is_file())
            self.assertFalse(staging.with_suffix(".matches.json").exists())
            self.assertEqual(catalog.history()["items"][0]["state"], "saved")
            self.assertEqual([item["action"] for item in sent], ["status", "record", "stop"])
            self.assertEqual(service.stop_take({"request_id": active["request_id"]}), receipt)
            draft = catalog.save_review_draft(receipt["take_id"], {"frames": 4, "points": [0]})
            self.assertTrue(draft["saved"])
            self.assertEqual(catalog.load_review_draft(receipt["take_id"])["master_sha256"], receipt["sha256"])
            destination.write_bytes(b"replaced master")
            with self.assertRaisesRegex(Conflict, "different master"):
                catalog.load_review_draft(receipt["take_id"])

    def test_failed_capture_is_not_shown_as_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            library.mkdir()
            catalog = Catalog(root / "state" / "catalog.sqlite", library)
            project = catalog.create_project("Fixture")

            def control(_path, packet):
                command = json.loads(packet)
                if command["action"] == "status":
                    return b'{"capture_generation":"g1","boot_generation":"b1"}'
                return b'{"error":"synthetic recorder overrun"}'

            service = AlbumService(catalog, root / "control.sock", send=control)
            with self.assertRaisesRegex(RuntimeError, "overrun"):
                service.start_take({"project_id": project["id"], "revision": 1, "side": "A"})
            self.assertEqual(catalog.history()["items"][0]["state"], "interrupted")

    def test_lost_start_and_stop_responses_remain_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            library.mkdir()
            catalog = Catalog(root / "state" / "catalog.sqlite", library)
            project = catalog.create_project("Fixture")
            active = {"id": None, "start_lost": True, "stop_timed_out": True}

            def control(_path, packet):
                command = json.loads(packet)
                if command["action"] == "status":
                    return json.dumps({"capture_generation": "g", "boot_generation": "b",
                                       "recording_id": active["id"]}).encode()
                if command["action"] == "record":
                    active["id"] = command["request_id"]
                    with wave.open(str(library / command["name"]), "wb") as writer:
                        writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                        writer.writeframes(b"\0\0\0\0" * 4)
                    if active["start_lost"]:
                        active["start_lost"] = False
                        raise TimeoutError("lost start response")
                    return b"{}"
                if command["action"] == "stop":
                    if active["stop_timed_out"]:
                        active["stop_timed_out"] = False
                        return b'{"error":"writer still finalizing"}'
                    active["id"] = None
                    return b'{"recorded_frames":4}'
                raise AssertionError(command)

            service = AlbumService(catalog, root / "control.sock", send=control)
            take = service.start_take({"project_id": project["id"], "revision": 1, "side": "A"})
            self.assertEqual(take["state"], "recording")
            self.assertEqual(catalog.get_intent(take["request_id"])["state"], "writing")
            with self.assertRaisesRegex(RuntimeError, "still finalizing"):
                service.stop_take({"request_id": take["request_id"]})
            self.assertEqual(catalog.get_intent(take["request_id"])["state"], "writing")
            receipt = service.stop_take({"request_id": take["request_id"]})
            self.assertEqual(receipt["frames"], 4)

    def test_invalid_closed_master_is_quarantined_and_releases_side(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            library.mkdir()
            catalog = Catalog(root / "state" / "catalog.sqlite", library)
            project = catalog.create_project("Fixture")
            def control(_path, packet):
                command = json.loads(packet)
                if command["action"] == "status":
                    return b'{"capture_generation":"g","boot_generation":"b"}'
                if command["action"] == "record":
                    (library / command["name"]).write_bytes(b"not a wav")
                    return b"{}"
                if command["action"] == "stop":
                    return b'{"recorded_frames":4}'
                raise AssertionError(command)

            service = AlbumService(catalog, root / "control.sock", send=control)
            take = service.start_take({"project_id": project["id"], "revision": 1, "side": "A"})
            with self.assertRaises(InvalidMaster):
                service.stop_take({"request_id": take["request_id"]})
            self.assertEqual(catalog.get_intent(take["request_id"])["state"], "quarantined")
            self.assertEqual(catalog.history()["items"][0]["state"], "interrupted")

    def test_catalog_service_records_and_publishes_real_synthetic_session_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            library.mkdir()
            catalog = Catalog(root / "state" / "catalog.sqlite", library)
            project = catalog.create_project("Synthetic Side")
            session = Session(library)

            def control(_path, packet):
                command = json.loads(packet)
                if command["action"] == "status":
                    return json.dumps(session.status()).encode()
                if command["action"] in ("record", "save_buffer"):
                    session.start_recording(command["name"], command["limit"],
                                            buffered=command["action"] == "save_buffer",
                                            request_id=command["request_id"], side=command["side"],
                                            buffered_start_frame=command.get("buffered_start_frame"),
                                            expected_capture_generation=command.get("expected_capture_generation"),
                                            overlap_ack=command.get("overlap_ack", False),
                                            side_a_boundary_frame=command.get("side_a_boundary_frame"))
                    return b"{}"
                if command["action"] == "stop":
                    frames = session.stop_recording(expected_id=command["request_id"])
                    latest = session.status()
                    return json.dumps({"recorded_frames": frames,
                                       "side_a_end_frame": latest["side_a_end_frame"],
                                       "side_a_end_capture_generation":
                                           latest["side_a_end_capture_generation"]}).encode()
                raise AssertionError(command)

            service = AlbumService(catalog, root / "capture.sock", send=control)
            take = service.start_take({"project_id": project["id"], "revision": 1, "side": "A"})
            pcm = b"\x00\x10\x00\x10" * 4800
            output = io.BytesIO()
            session.run(io.BytesIO(pcm), output)
            receipt = service.stop_take({"request_id": take["request_id"]})
            master = library / receipt["locator"]
            with wave.open(str(master), "rb") as saved:
                self.assertEqual(saved.getnframes(), 4800)
                self.assertEqual(saved.readframes(4800), pcm)
            self.assertEqual(output.getvalue(), pcm)
            generation = session.status()["capture_generation"]
            boundary = catalog.get_side_boundary(project["id"], generation)
            self.assertEqual(boundary["end_frame"], 4800)
            self.assertEqual(receipt["side_a_end_frame"], boundary["end_frame"])
            with catalog.connect() as db:
                row = db.execute("SELECT side_a_end_frame FROM intents WHERE request_id=?",
                                 (take["request_id"],)).fetchone()
            self.assertEqual(row[0], boundary["end_frame"])

            reopened = Catalog(root / "state" / "catalog.sqlite", library)
            self.assertEqual(reopened.get_side_boundary(project["id"], generation)["end_frame"], 4800)
            session.run(io.BytesIO(pcm), io.BytesIO())
            side_b = AlbumService(reopened, root / "capture.sock", send=control)
            take_b = side_b.start_take({"project_id": project["id"], "revision": 1,
                                        "side": "B", "buffered": True})
            # The saved Side A boundary is the default start despite the
            # additional audio already retained after it.
            self.assertEqual(session.status()["recording_prefix_frames"], 4800)
            receipt_b = side_b.stop_take({"request_id": take_b["request_id"]})
            self.assertEqual(receipt_b["frames"], 4800)


if __name__ == "__main__":
    unittest.main()
