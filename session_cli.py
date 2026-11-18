"""Local control socket for a Record Relay PCM session."""
import argparse
import json
import os
from pathlib import Path
import socket
from socketserver import StreamRequestHandler, UnixStreamServer
import sys
from time import monotonic, time
from threading import Event, Lock, Thread
import uuid

from session import Session
from providers import authenticated_open, recognize_audd, shazam_recognizer, tls_transport
from worker import RecognitionWorker, ScrobbleDelivery
from listening import ListeningCoordinator, PlaybackTiming
from music_assistant import MusicAssistantObserver
from receipts import ReceiptStore


def private_text(path):
    details = path.lstat()
    parent = path.parent.stat()
    if (path.is_symlink() or not path.is_file() or details.st_uid != os.getuid()
            or details.st_mode & 0o077 or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise ValueError("Use an owner-only file in an owner-only directory")
    return path.read_text()


def still_listening(session, track, local, expected_epoch=None):
    status = session.status()
    current = status.get("track")
    if not isinstance(current, dict) or not isinstance(track, dict):
        return False
    identity = lambda value: (value.get("artist", "").casefold().strip(),
                              value.get("title", "").casefold().strip())
    return (status["scrobble"] and status["signal"] and identity(current) == identity(track)
            and (expected_epoch is None or status["track_epoch"] == expected_epoch)
            and status["local_listening"] == local)


def serve(args):
    if not args.library.is_dir():
        raise ValueError("Create the recording directory first")
    info = args.socket.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The control socket needs an owner-only directory")
    session = Session(args.library)
    visual_clients = {}
    visual_lock = Lock()
    visual_stop = Event()

    def expire_visual_clients():
        while not visual_stop.wait(0.5):
            now = monotonic()
            with visual_lock:
                for client_id, expiry in list(visual_clients.items()):
                    if expiry <= now:
                        visual_clients.pop(client_id, None)
                desired = bool(visual_clients)
            session.set_live_view(desired)

    visual_thread = Thread(target=expire_visual_clients, daemon=True, name="record-relay-visual-leases")
    visual_thread.start()
    token = private_text(args.audd_token_file).strip() if args.audd_token_file else None
    if token is not None and not token.isalnum():
        raise ValueError("Invalid AudD token file")
    recognizer = None
    if token is not None:
        recognizer = lambda sample: recognize_audd(sample, token, enabled=True)
    elif args.shazam:
        recognizer = shazam_recognizer(use_catalog=args.shazam_catalog_duration)
    recognition_worker = scrobble_worker = None
    monitor = None
    if args.scrobble_config:
        config = json.loads(private_text(args.scrobble_config))
        transport = authenticated_open
        observer = None
        ma_keys = ("music_assistant_url", "music_assistant_token", "player_id", "source_uri")
        if any(config.get(key) for key in ma_keys):
            if not all(config.get(key) for key in ma_keys):
                raise ValueError("Music Assistant observer needs URL, token, player ID, and source URI")
            if tls := config.get("client_tls"):
                if not config["music_assistant_url"].startswith("https://"):
                    raise ValueError("Client TLS requires an HTTPS Music Assistant URL")
                transport = tls_transport(tls["authority"], tls["certificate"], tls["key"])
            observer = MusicAssistantObserver(config["music_assistant_url"], config["music_assistant_token"],
                                              config["player_id"], config["source_uri"], transport=transport)
        listenbrainz_token = config.get("listenbrainz_token")
        if not isinstance(listenbrainz_token, str) or not listenbrainz_token.strip():
            raise ValueError("A ListenBrainz token is required for scrobbling")
        receipt_path = args.scrobble_config.parent / "listens.sqlite"
        if receipt_path.is_symlink():
            raise ValueError("Receipt database must not be a symbolic link")
        monitor = ListeningCoordinator(observer, ScrobbleDelivery(ReceiptStore(receipt_path),
                                         listenbrainz_token, timing=PlaybackTiming(clock=monotonic)),
                                       wall_clock=time)

    class Handler(StreamRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            try:
                data = json.loads(self.rfile.readline(4096))
                action = data["action"]
                client_id = data.get("visual_client_id")
                if client_id is not None:
                    try:
                        client_id = str(uuid.UUID(client_id))
                    except (ValueError, TypeError, AttributeError) as error:
                        raise ValueError("Invalid live view client ID") from error
                    with visual_lock:
                        visual_clients[client_id] = monotonic() + 5
                if action == "identify":
                    if type(data["enabled"]) is not bool:
                        raise ValueError("Identify expects a boolean")
                    if data["enabled"] and recognizer is None:
                        raise ValueError("Start the session with a configured recognition provider")
                    session.set_identify(data["enabled"])
                elif action == "scrobble":
                    if type(data["enabled"]) is not bool:
                        raise ValueError("Scrobble expects a boolean")
                    if data["enabled"] and monitor is None:
                        raise ValueError("Configure ListenBrainz first")
                    session.set_scrobble(data["enabled"])
                elif action == "local_listening":
                    if type(data["enabled"]) is not bool:
                        raise ValueError("Local listening expects a boolean")
                    session.set_local_listening(data["enabled"])
                elif action == "visual_client":
                    if type(data["enabled"]) is not bool:
                        raise ValueError("Live view client expects a boolean")
                    if client_id is None:
                        raise ValueError("A live view client ID is required")
                    with visual_lock:
                        if data["enabled"]:
                            visual_clients[client_id] = monotonic() + 5
                        else:
                            visual_clients.pop(client_id, None)
                        desired = bool(visual_clients)
                    session.set_live_view(desired)
                elif action == "offline_queue":
                    if type(data["enabled"]) is not bool:
                        raise ValueError("Offline queue expects a boolean")
                    if monitor is None and data["enabled"]:
                        raise ValueError("Configure ListenBrainz first")
                    if monitor is not None:
                        monitor.delivery.queue_enabled = data["enabled"]
                        if not data["enabled"]:
                            monitor.delivery.cancel_queued()
                elif action == "delete_queued_listen":
                    play_id = data.get("play_id")
                    if not isinstance(play_id, str) or len(play_id) > 80 or monitor is None:
                        raise ValueError("Invalid queued listen ID")
                    if not monitor.delivery.delete_queued(play_id):
                        raise ValueError("This item is no longer queued or is already being sent")
                elif action in ("record", "save_buffer"):
                    session.start_recording(data["name"], data.get("limit", 1800),
                                            buffered=action == "save_buffer",
                                            request_id=data.get("request_id"),
                                            side=data.get("side"),
                                            buffered_start_frame=data.get("buffered_start_frame"),
                                            expected_capture_generation=data.get("expected_capture_generation"),
                                            overlap_ack=data.get("overlap_ack", False),
                                            side_a_boundary_frame=data.get("side_a_boundary_frame"))
                elif action == "stop":
                    frames = session.stop_recording(expected_id=data.get("request_id"))
                    latest = session.status()
                    self.wfile.write(json.dumps({"recorded_frames": frames,
                                                 "side_a_end_frame": latest.get("side_a_end_frame"),
                                                 "side_a_end_capture_generation": latest.get(
                                                     "side_a_end_capture_generation")}).encode() + b"\n")
                    return
                elif action != "status":
                    raise ValueError("Unknown action")
                result = session.status()
                with visual_lock:
                    result["live_view_clients"] = len(visual_clients)
                    result["live_view_client"] = client_id in visual_clients if client_id else False
                if recognition_worker is not None:
                    result["recognition_error"] = recognition_worker.error
                if scrobble_worker is not None:
                    result["scrobble_error"] = scrobble_worker.error
                    result["scrobble_progress"] = monitor.progress(result)
                if monitor is not None:
                    result["offline_queue"] = monitor.delivery.queue_enabled
                    result["listen_queue"] = monitor.delivery.queued()
                    result["listenbrainz_clock_trusted"] = monitor.clock_trusted
                else:
                    result["offline_queue"] = False
                    result["listen_queue"] = []
            except (ValueError, KeyError, TypeError, AttributeError, RuntimeError, OSError) as error:
                result = {"error": str(error)}
            self.wfile.write(json.dumps(result).encode() + b"\n")

    with UnixStreamServer(str(args.socket), Handler) as server:
        if recognizer is not None:
            recognition_worker = RecognitionWorker(
                lambda: session.lookup_if_due(recognizer),
                lambda: session.status()["identify"],
            )
        if monitor is not None:
            monitor.delivery.queue_enabled = bool(config.get("offline_queue", False))
            scrobble_worker = RecognitionWorker(
                lambda: monitor.tick(
                    session.status(),
                    lambda track, local: still_listening(session, track, local, monitor.track_epoch),
                    queue_consent=lambda: bool(session.status().get("scrobble"))),
                lambda: True, interval=1,
            )
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            session.run(sys.stdin.buffer, sys.stdout.buffer)
        finally:
            server.shutdown()
            if recognition_worker is not None:
                recognition_worker.stop()
            if scrobble_worker is not None:
                scrobble_worker.stop()
            try:
                session.stop_recording()
            finally:
                visual_stop.set()
                visual_thread.join(1)
                session.set_live_view(False)
                session.close_live_view()
                args.socket.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    start = modes.add_parser("serve")
    start.add_argument("socket", type=Path)
    start.add_argument("library", type=Path)
    providers = start.add_mutually_exclusive_group()
    providers.add_argument("--audd-token-file", type=Path)
    providers.add_argument("--shazam", action="store_true")
    start.add_argument("--shazam-catalog-duration", action="store_true")
    start.add_argument("--scrobble-config", type=Path)
    control = modes.add_parser("control")
    control.add_argument("socket", type=Path)
    control.add_argument("request", help="JSON action")
    args = parser.parse_args()
    if args.mode == "serve":
        if args.shazam_catalog_duration and not args.shazam:
            parser.error("Catalog duration requires the Shazam adapter")
        serve(args)
    else:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(args.socket))
            client.sendall(args.request.encode() + b"\n")
            print(client.makefile().readline(), end="")


if __name__ == "__main__":
    main()
