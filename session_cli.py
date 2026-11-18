"""Local control socket for a Record Relay PCM session."""
import argparse
import json
import os
from pathlib import Path
import socket
from socketserver import StreamRequestHandler, UnixStreamServer
import sys
from time import time
from threading import Thread

from session import Session
from providers import authenticated_open, recognize_audd, shazam_recognizer, tls_transport
from worker import RecognitionWorker, ScrobbleDelivery
from music_assistant import MusicAssistantObserver, ScrobbleMonitor
from receipts import ReceiptStore


def private_text(path):
    details = path.lstat()
    parent = path.parent.stat()
    if (path.is_symlink() or not path.is_file() or details.st_uid != os.getuid()
            or details.st_mode & 0o077 or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise ValueError("Use an owner-only file in an owner-only directory")
    return path.read_text()


def serve(args):
    if not args.library.is_dir():
        raise ValueError("Create the recording directory first")
    info = args.socket.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The control socket needs an owner-only directory")
    session = Session(args.library)
    token = private_text(args.audd_token_file).strip() if args.audd_token_file else None
    if token is not None and not token.isalnum():
        raise ValueError("Invalid AudD token file")
    recognizer = None
    if token is not None:
        recognizer = lambda sample: recognize_audd(sample, token, enabled=True)
    elif args.shazam:
        recognizer = shazam_recognizer()
    recognition_worker = scrobble_worker = None
    monitor = None
    if args.scrobble_config:
        config = json.loads(private_text(args.scrobble_config))
        transport = authenticated_open
        if tls := config.get("client_tls"):
            if not config["music_assistant_url"].startswith("https://"):
                raise ValueError("Client TLS requires an HTTPS Music Assistant URL")
            transport = tls_transport(tls["authority"], tls["certificate"], tls["key"])
        observer = MusicAssistantObserver(config["music_assistant_url"], config["music_assistant_token"],
                                          config["player_id"], config["source_uri"], transport=transport)
        receipt_path = args.scrobble_config.parent / "listens.sqlite"
        if receipt_path.is_symlink():
            raise ValueError("Receipt database must not be a symbolic link")
        monitor = ScrobbleMonitor(observer, ScrobbleDelivery(ReceiptStore(receipt_path),
                                  config["listenbrainz_token"]), wall_clock=time)

    class Handler(StreamRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            try:
                data = json.loads(self.rfile.readline(4096))
                action = data["action"]
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
                        raise ValueError("Configure Music Assistant and ListenBrainz first")
                    session.set_scrobble(data["enabled"])
                elif action == "record":
                    session.start_recording(data["name"], data.get("limit", 1800))
                elif action == "stop":
                    frames = session.stop_recording()
                    self.wfile.write(json.dumps({"recorded_frames": frames}).encode() + b"\n")
                    return
                elif action != "status":
                    raise ValueError("Unknown action")
                result = session.status()
                if recognition_worker is not None:
                    result["recognition_error"] = recognition_worker.error
                if scrobble_worker is not None:
                    result["scrobble_error"] = scrobble_worker.error
                    result["scrobble_progress"] = monitor.progress(result)
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
            scrobble_worker = RecognitionWorker(
                lambda: monitor.tick(session.status(), lambda track: session.status()["scrobble"] and session.status()["track"] is track),
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
    start.add_argument("--scrobble-config", type=Path)
    control = modes.add_parser("control")
    control.add_argument("socket", type=Path)
    control.add_argument("request", help="JSON action")
    args = parser.parse_args()
    if args.mode == "serve":
        serve(args)
    else:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(10)
            client.connect(str(args.socket))
            client.sendall(args.request.encode() + b"\n")
            print(client.makefile().readline(), end="")


if __name__ == "__main__":
    main()
