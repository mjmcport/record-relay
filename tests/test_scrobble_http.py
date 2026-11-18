from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.request import Request, urlopen

from music_assistant import MusicAssistantObserver, ScrobbleMonitor
from providers import send_listen
from receipts import ReceiptStore
from worker import PlaybackTiming, ScrobbleDelivery


class ScrobbleHTTPTests(unittest.TestCase):
    def test_locally_stubbed_service_receives_one_eligible_listen(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                packet = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api":
                    response = [{"player_id": "test-sink", "active_source": "source:test",
                                 "type": "player", "available": True, "volume_muted": False,
                                 "volume_level": 30, "playback_state": "playing"}]
                else:
                    received.append((self.path, self.headers["Authorization"], packet))
                    response = {"status": "ok"}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

            def log_message(self, *_):
                pass

        with TemporaryDirectory() as folder, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"

            def local_stub(request, timeout):
                self.assertEqual(request.full_url, "https://api.listenbrainz.org/1/submit-listens")
                return urlopen(Request(url + "/1/submit-listens", request.data,
                                       dict(request.header_items())), timeout=timeout)

            try:
                observer = MusicAssistantObserver(url, "mock-ma", "test-sink", "source:test",
                                                  transport=urlopen)
                now = iter(range(250))
                delivery = ScrobbleDelivery(
                    ReceiptStore(Path(folder) / "receipts.sqlite"), "mock-lb",
                    timing=PlaybackTiming(clock=lambda: next(now)),
                    sender=partial(send_listen, transport=local_stub),
                )
                monitor = ScrobbleMonitor(observer, delivery, wall_clock=lambda: 1000)
                state = {"scrobble": True, "signal": True,
                         "track": {"artist": "Artist", "title": "Song", "album": "Record"}}
                for _ in range(245):
                    monitor.tick(state, lambda _: True)
                self.assertEqual(len(received), 1)
                path, token, packet = received[0]
                self.assertEqual((path, token), ("/1/submit-listens", "Token mock-lb"))
                self.assertEqual(packet["payload"][0]["track_metadata"]["release_name"], "Record")
            finally:
                server.shutdown()
                thread.join()
