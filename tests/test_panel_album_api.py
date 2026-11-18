import json
from pathlib import Path
import re
import socket
import tempfile
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import wave
import unittest
from http.server import ThreadingHTTPServer

from album_service import AlbumService
from catalog import Catalog
from panel import panel_handler


class AlbumPanelAPITests(unittest.TestCase):
    def test_local_ui_uses_authenticated_catalog_and_capture_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            library.mkdir()
            catalog = Catalog(root / "private" / "catalog.sqlite", library)

            def control(_path, packet):
                command = json.loads(packet)
                if command["action"] == "status":
                    return b'{"capture_generation":"capture","boot_generation":"boot"}'
                if command["action"] == "record":
                    with wave.open(str(library / command["name"]), "wb") as writer:
                        writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                        writer.writeframes(b"\0\0\0\0" * 8)
                    return b"{}"
                if command["action"] == "stop":
                    return b'{"recorded_frames":8}'
                raise AssertionError(command)

            album_service = AlbumService(catalog, root / "capture.sock", send=control)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            server = ThreadingHTTPServer(("127.0.0.1", port), panel_handler(
                root / "capture.sock", port, library, album_service=album_service))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{port}"
            origin = base
            try:
                with urlopen(base + "/album/") as response:
                    page = response.read().decode()
                token = re.search(r'<meta name="control-token" content="([^"]+)"', page).group(1)
                self.assertIn("/album/album.mjs", page)
                self.assertIn("Local workbench", page)
                self.assertEqual(json.load(urlopen(base + "/api/album"))["projects"], [])

                def post(path, body, *, csrf=True, origin_header=origin):
                    headers = {"Content-Type": "application/json", "Origin": origin_header}
                    if csrf:
                        headers["X-Control-Token"] = token
                    return urlopen(Request(base + path, data=json.dumps(body).encode(), headers=headers))

                with self.assertRaises(HTTPError) as denied:
                    post("/api/projects", {"label": "Blocked"}, csrf=False)
                self.assertEqual(denied.exception.code, 403)
                with self.assertRaises(HTTPError) as wrong_origin:
                    post("/api/projects", {"label": "Blocked"}, origin_header="http://evil.invalid")
                self.assertEqual(wrong_origin.exception.code, 403)

                with post("/api/projects", {"label": "Synthetic Record"}) as response:
                    project = json.load(response)
                with post("/api/takes/start", {"project_id": project["id"], "revision": 1, "side": "A"}) as response:
                    active = json.load(response)
                self.assertEqual(catalog.reconcile(live_request_ids={active["request_id"]}), [])
                self.assertEqual(catalog.get_intent(active["request_id"])["state"], "writing")
                with post("/api/takes/stop", {"request_id": active["request_id"]}) as response:
                    receipt = json.load(response)
                self.assertEqual(receipt["frames"], 8)
                with post("/recordings/draft", {"name": receipt["locator"], "draft": {"frames": 8, "points": [0]}}) as response:
                    saved_draft = json.load(response)
                self.assertTrue(saved_draft["saved"])
                with post("/recordings/draft", {"name": receipt["locator"]}) as response:
                    restored_draft = json.load(response)
                self.assertEqual(restored_draft["points"], [0])
                self.assertEqual(restored_draft["master_sha256"], receipt["sha256"])
                data = json.load(urlopen(base + "/api/album"))
                self.assertEqual(data["history"]["items"][0]["state"], "saved")
                self.assertEqual(data["history"]["items"][0]["side"], "A")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
