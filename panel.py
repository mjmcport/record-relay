"""Serve the local Record Relay control panel on loopback."""
import argparse
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


def panel_handler(path, port):
    address = f"127.0.0.1:{port}"
    token = secrets.token_urlsafe(32)
    html = Path(__file__).with_name("panel.html").read_text().replace("__CONTROL_TOKEN__", token)

    class Handler(BaseHTTPRequestHandler):
        def send_body(self, status, content_type, content):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if self.path != "/" or self.headers.get("Host") != address:
                self.send_error(404)
                return
            self.send_body(200, "text/html; charset=utf-8", html.encode())

        def do_POST(self):
            if (self.path != "/control" or self.headers.get("Host") != address
                    or self.headers.get("Origin") != f"http://{address}"
                    or not secrets.compare_digest(self.headers.get("X-Control-Token", ""), token)):
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
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), panel_handler(args.socket, args.port)) as server:
        server.serve_forever()
