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


def serve(args):
    if not args.library.is_dir():
        raise ValueError("Create the recording directory first")
    info = args.socket.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The control socket needs an owner-only directory")
    session = Session(args.library)

    class Handler(StreamRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            try:
                data = json.loads(self.rfile.readline(4096))
                action = data["action"]
                if action == "identify":
                    if type(data["enabled"]) is not bool:
                        raise ValueError("Identify expects a boolean")
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
            except (ValueError, KeyError, TypeError, AttributeError, RuntimeError, OSError) as error:
                result = {"error": str(error)}
            self.wfile.write(json.dumps(result).encode() + b"\n")

    with UnixStreamServer(str(args.socket), Handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            session.run(sys.stdin.buffer, sys.stdout.buffer)
        finally:
            server.shutdown()
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
