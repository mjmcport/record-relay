"""Local control socket for a Record Relay PCM session."""
import argparse
import json
import os
from pathlib import Path
import socket
from socketserver import StreamRequestHandler, UnixStreamServer
import sys
from threading import Thread

from session import Session
from providers import recognize_audd
from worker import RecognitionWorker


def serve(args):
    if not args.library.is_dir():
        raise ValueError("Create the recording directory first")
    info = args.socket.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The control socket needs an owner-only directory")
    session = Session(args.library)
    token = None
    if args.audd_token_file:
        details = args.audd_token_file.lstat()
        if (args.audd_token_file.is_symlink() or not args.audd_token_file.is_file()
                or details.st_uid != os.getuid() or details.st_mode & 0o077):
            raise ValueError("Use a private, owner-only AudD token file")
        token = args.audd_token_file.read_text().strip()
        if not token or not token.isalnum():
            raise ValueError("Invalid AudD token file")
    recognition_worker = None

    class Handler(StreamRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            try:
                data = json.loads(self.rfile.readline(4096))
                action = data["action"]
                if action == "identify":
                    if type(data["enabled"]) is not bool:
                        raise ValueError("Identify expects a boolean")
                    if data["enabled"] and token is None:
                        raise ValueError("Start the session with a configured recognition provider")
                    session.set_identify(data["enabled"])
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
            except (ValueError, KeyError, TypeError, AttributeError, RuntimeError, OSError) as error:
                result = {"error": str(error)}
            self.wfile.write(json.dumps(result).encode() + b"\n")

    with UnixStreamServer(str(args.socket), Handler) as server:
        if token is not None:
            recognition_worker = RecognitionWorker(
                lambda: session.lookup_if_due(
                    lambda sample: recognize_audd(sample, token, enabled=True)),
                lambda: session.status()["identify"],
            )
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            session.run(sys.stdin.buffer, sys.stdout.buffer)
        finally:
            server.shutdown()
            if recognition_worker is not None:
                recognition_worker.stop()
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
    start.add_argument("--audd-token-file", type=Path)
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
