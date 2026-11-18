import json
from pathlib import Path
import socket
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import session_cli


class SessionCliVisualTests(unittest.TestCase):
    def test_visual_client_leases_support_multiple_tabs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            library = root / "library"
            library.mkdir()
            stop = Event()
            state = {"live": False}

            class FakeSession:
                def __init__(self, _library):
                    pass

                def set_live_view(self, enabled):
                    state["live"] = enabled

                def status(self):
                    return {"live_view": state["live"], "recording": "idle"}

                def run(self, _source, _sink):
                    stop.wait(10)

                def stop_recording(self):
                    return 0

                def close_live_view(self):
                    pass

            args = type("Args", (), {"library": library, "socket": root / "runtime" / "control.sock",
                                      "audd_token_file": None, "shazam": False,
                                      "shazam_catalog_duration": False, "scrobble_config": None})()
            args.socket.parent.mkdir(mode=0o700)
            errors = []

            def serve():
                try:
                    session_cli.serve(args)
                except Exception as error:  # surface background failures in the test thread
                    errors.append(error)

            with patch.object(session_cli, "Session", FakeSession):
                thread = Thread(target=serve)
                thread.start()
                try:
                    for _ in range(100):
                        if args.socket.exists():
                            break
                        time.sleep(0.01)
                    self.assertTrue(args.socket.exists(), f"control socket did not start: {errors!r}")
                    client_a, client_b = str(uuid4()), str(uuid4())

                    def request(payload):
                        with socket.socket(socket.AF_UNIX) as connection:
                            connection.settimeout(2)
                            connection.connect(str(args.socket))
                            connection.sendall(json.dumps(payload).encode() + b"\n")
                            return json.loads(connection.makefile("rb").readline())

                    result = request({"action": "visual_client", "enabled": True,
                                      "visual_client_id": client_a})
                    self.assertTrue(result["live_view"])
                    result = request({"action": "visual_client", "enabled": True,
                                      "visual_client_id": client_b})
                    self.assertEqual(result["live_view_clients"], 2)
                    client_c, client_d, client_e = str(uuid4()), str(uuid4()), str(uuid4())
                    request({"action": "visual_client", "enabled": True, "visual_client_id": client_c})
                    request({"action": "visual_client", "enabled": True, "visual_client_id": client_d})
                    result = request({"action": "visual_client", "enabled": True, "visual_client_id": client_e})
                    self.assertIn("four browser tabs", result["error"])
                    result = request({"action": "visual_client", "enabled": False,
                                      "visual_client_id": client_a})
                    self.assertTrue(result["live_view"])
                    self.assertEqual(result["live_view_clients"], 3)
                    result = request({"action": "visual_client", "enabled": False,
                                      "visual_client_id": client_b})
                    self.assertTrue(result["live_view"])
                    for client_id in (client_c, client_d):
                        result = request({"action": "visual_client", "enabled": False,
                                          "visual_client_id": client_id})
                    self.assertFalse(result["live_view"])
                    self.assertEqual(result["live_view_clients"], 0)
                finally:
                    stop.set()
                    thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
