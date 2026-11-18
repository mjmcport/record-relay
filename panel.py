"""Serve the local Record Relay control panel on loopback."""
import argparse
from html import escape
import os
import re
import shutil
import stat
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import socket


def forward(path, packet):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(10)
        connection.connect(str(path))
        connection.sendall(packet + b"\n")
        return connection.makefile().readline(64_000).encode()


def panel_handler(path, port, library=None):
    address = f"127.0.0.1:{port}"
    token = secrets.token_urlsafe(32)
    directory = Path(__file__).parent
    link = '<a href="/recordings/">Download recordings</a> · ' if library is not None else ""
    html = (directory / "panel.html").read_text().replace("__CONTROL_TOKEN__", token).replace("__RECORDINGS_LINK__", link)
    pages = {
        "/": ("text/html; charset=utf-8", html.encode()),
        "/review/": ("text/html; charset=utf-8", (directory / "review/index.html").read_bytes()),
        "/review/review.mjs": ("text/javascript; charset=utf-8", (directory / "review/review.mjs").read_bytes()),
        "/review/splits.mjs": ("text/javascript; charset=utf-8", (directory / "review/splits.mjs").read_bytes()),
    }

    def recording_name(name):
        return re.fullmatch(r"[a-zA-Z0-9_. -]{1,120}\.wav", name, re.IGNORECASE) and not name.startswith(".")

    class Handler(BaseHTTPRequestHandler):
        def recordings(self):
            entries = []
            for file in sorted(library.iterdir()):
                if recording_name(file.name) and file.is_file() and not file.is_symlink():
                    name = escape(file.name, quote=True)
                    entries.append(f'<li><button name="name" value="{name}">Download {name}</button></li>')
            content = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Record Relay · Recordings</title><main><h1>Recordings</h1>
<p><a href="/">Capture controls</a> · <a href="/review/">Review track splits</a></p>
<form action="/recordings/download" method="post">
<input name="token" type="hidden" value="{token}">
<ul>{''.join(entries) or '<li>No finished recordings yet.</li>'}</ul></form></main></html>"""
            self.send_body(200, "text/html; charset=utf-8", content.encode())

        def download(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError
                form = parse_qs(self.rfile.read(length).decode(), strict_parsing=True, max_num_fields=2)
                name, = form["name"]
                supplied_token, = form["token"]
                if not secrets.compare_digest(supplied_token, token):
                    self.send_error(403)
                    return
                if not recording_name(name):
                    raise ValueError
                fd = os.open(library / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except (KeyError, ValueError, TypeError, OSError):
                self.send_error(404)
                return
            with os.fdopen(fd, "rb") as recording:
                info = os.fstat(recording.fileno())
                if not stat.S_ISREG(info.st_mode):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(info.st_size))
                self.end_headers()
                shutil.copyfileobj(recording, self.wfile)

        def send_body(self, status, content_type, content):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if self.headers.get("Host") != address:
                self.send_error(404)
                return
            if self.path == "/recordings/" and library is not None:
                self.recordings()
                return
            if self.path not in pages:
                self.send_error(404)
                return
            self.send_body(200, *pages[self.path])

        def do_POST(self):
            if (self.headers.get("Host") != address
                    or self.headers.get("Origin") != f"http://{address}"):
                self.send_error(403)
                return
            if self.path == "/recordings/download" and library is not None:
                self.download()
                return
            if self.path != "/control" or not secrets.compare_digest(self.headers.get("X-Control-Token", ""), token):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size < 4096:
                    raise ValueError
                response = forward(path, self.rfile.read(size))
                if not response:
                    raise OSError
            except (ValueError, OSError):
                self.send_body(503, "application/json", b'{"error":"Could not reach the capture session."}')
                return
            self.send_body(200, "application/json", response)

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", type=Path)
    parser.add_argument("--port", type=int, default=8766, choices=range(1024, 65536), metavar="PORT")
    parser.add_argument("--library", type=Path, help="Directory containing finished WAV recordings")
    args = parser.parse_args()
    if args.library is not None and (args.library.is_symlink() or not args.library.is_dir()):
        parser.error("Provide a real recording directory")
    with ThreadingHTTPServer(("127.0.0.1", args.port), panel_handler(args.socket, args.port, args.library)) as server:
        server.serve_forever()
