"""Mutual-TLS gateway for one Record Relay appliance and playback target."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
from urllib.request import Request

try:
    from .project_metadata import project
except ImportError:
    from project_metadata import project

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from providers import authenticated_open, request_json


def make_handler(player_id, snapshot, fetch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.connection.settimeout(5)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError
                packet = json.loads(self.rfile.read(length))
                if self.path == "/metadata" and isinstance(packet, dict):
                    project(snapshot, packet)
                    response = {"ok": True}
                elif self.path == "/api" and isinstance(packet, dict) and packet.get("command") == "players/all":
                    token = self.headers.get("Authorization", "")
                    if not token.startswith("Bearer ") or not 7 < len(token) <= 2048:
                        self.send_error(401)
                        return
                    players = fetch(token)
                    if not isinstance(players, list):
                        raise ValueError("Invalid playback response")
                    fields = ("player_id", "type", "available", "playback_state", "active_source",
                              "volume_muted", "volume_level", "group_childs")
                    response = [{key: player.get(key) for key in fields} for player in players
                                if isinstance(player, dict) and player.get("player_id") == player_id]
                else:
                    self.send_error(404)
                    return
            except (OSError, ValueError, TypeError):
                self.send_error(502)
                return
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def music_assistant(token):
    packet = {"command": "players/all", "args": {}}
    request = Request("http://127.0.0.1:8095/api", json.dumps(packet).encode(),
                      {"Content-Type": "application/json", "Authorization": token})
    return request_json(request, authenticated_open)


def serve(host, port, certificate, key, authority, player, snapshot, fetch=music_assistant):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    context.load_verify_locations(cafile=str(authority))
    context.verify_mode = ssl.CERT_REQUIRED
    class SecureServer(ThreadingHTTPServer):
        def finish_request(self, request, client_address):
            request.settimeout(5)
            try:
                secured = context.wrap_socket(request, server_side=True)
            except (ssl.SSLError, TimeoutError):
                return
            with secured:
                super().finish_request(secured, client_address)

    return SecureServer((host, port), make_handler(player, snapshot, fetch))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=18195)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--authority", type=Path, required=True)
    parser.add_argument("--player", required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    with serve(args.host, args.port, args.certificate, args.key,
               args.authority, args.player, args.snapshot) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
