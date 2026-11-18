"""Publish only recognized track metadata from the Pi over mutual TLS."""
import argparse
import json
from pathlib import Path
import sys
from time import sleep
from urllib.parse import urlsplit
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panel import forward
from providers import tls_transport, request_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", type=Path)
    parser.add_argument("gateway")
    parser.add_argument("authority", type=Path)
    parser.add_argument("certificate", type=Path)
    parser.add_argument("key", type=Path)
    args = parser.parse_args()
    address = urlsplit(args.gateway)
    if (address.scheme != "https" or not address.hostname or address.username
            or address.password or address.fragment or address.query or address.path not in ("", "/")):
        parser.error("Use a dedicated HTTPS gateway origin.")
    transport = tls_transport(args.authority, args.certificate, args.key)
    endpoint = args.gateway.rstrip("/") + "/metadata"
    while True:
        try:
            state = json.loads(forward(args.socket, b'{"action":"status"}'))
            track = state.get("track")
            if state.get("identify") and state.get("signal") and isinstance(track, dict):
                track = {key: value[:300] for key in ("title", "artist", "album")
                         if isinstance(value := track.get(key), str)}
            else:
                track = None
            packet = {"identify": track is not None, "signal": track is not None, "track": track}
            request_json(Request(endpoint, json.dumps(packet).encode(),
                                 {"Content-Type": "application/json"}), transport)
        except (OSError, ValueError, TypeError):
            pass
        sleep(3)


if __name__ == "__main__":
    main()
