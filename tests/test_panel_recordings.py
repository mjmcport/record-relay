from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
import wave
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import patch

from panel import panel_handler


class RecordingDownloads(unittest.TestCase):
    def test_fingerprint_endpoint_requires_noncommercial_acknowledgment(self):
        with TemporaryDirectory() as directory:
            library = Path(directory)
            with wave.open(str(library / "side.wav"), "wb") as writer:
                writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                writer.writeframes(bytes(4 * 10 * 48000))
            with ThreadingHTTPServer(("127.0.0.1", 0), panel_handler) as server:
                server.RequestHandlerClass = panel_handler(
                    library / "control.sock", server.server_port, library,
                    acoustid_client_key="abcdefgh12345678",
                )
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"http://127.0.0.1:{server.server_port}"
                    with urlopen(url + "/review/?side=side.wav", timeout=4) as response:
                        page = response.read().decode()
                        cookie = response.headers["Set-Cookie"].split(";")[0]
                    token = re.search(r'data-review-token="([^"]+)"', page)[1]

                    def lookup(ack):
                        body = json.dumps({"name": "side.wav", "start": 0, "end": 480_000,
                                           "sample_position": "middle",
                                           "noncommercial_ack": ack}).encode()
                        return urlopen(Request(url + "/recordings/fingerprint", body, {
                            "Origin": url, "Cookie": cookie, "X-Control-Token": token,
                            "Content-Type": "application/json",
                        }), timeout=4)

                    with patch("fingerprinting.identify_saved_segment",
                               return_value={"duration_seconds": 10, "matches": []}) as identify:
                        with self.assertRaises(HTTPError) as error:
                            lookup(False)
                        self.assertEqual(error.exception.code, 400)
                        error.exception.close()
                        self.assertFalse(identify.called)
                        with lookup(True) as response:
                            self.assertEqual(json.loads(response.read())["matches"], [])
                        with self.assertRaises(HTTPError) as error:
                            lookup(True)
                        self.assertEqual(error.exception.code, 429)
                        error.exception.close()
                        identify.assert_called_once_with(
                            library / "side.wav", 0, 480_000, "abcdefgh12345678",
                            sample_position="middle", fpcalc="fpcalc")
                finally:
                    server.shutdown()
                    thread.join()

    def test_download_requires_origin_and_token_and_cannot_read_symlink(self):
        with TemporaryDirectory() as directory:
            library = Path(directory)
            (library / "side A.wav").write_bytes(b"RIFF-test")
            (library / "shortcut.wav").symlink_to(library / "side A.wav")
            with ThreadingHTTPServer(("127.0.0.1", 0), panel_handler) as server:
                server.RequestHandlerClass = panel_handler(
                    library / "control.sock", server.server_port, library
                )
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"http://127.0.0.1:{server.server_port}"
                    with urlopen(url + "/recordings/", timeout=4) as response:
                        page = response.read().decode()
                    self.assertIn("Download side A.wav", page)
                    self.assertNotIn("shortcut.wav", page)
                    token = re.search(r'name="token" type="hidden" value="([^"]+)"', page)[1]

                    def download(name, supplied_token, origin=url):
                        body = urlencode({"name": name, "token": supplied_token}).encode()
                        return urlopen(Request(url + "/recordings/download", body,
                                               {"Origin": origin}), timeout=4)

                    with download("side A.wav", token) as response:
                        self.assertEqual(response.headers["Content-Disposition"],
                                         'attachment; filename="side A.wav"')
                        self.assertEqual(response.read(), b"RIFF-test")
                    for name, key, origin, status in [
                        ("side A.wav", token, "https://elsewhere.invalid", 403),
                        ("side A.wav", "wrong", url, 403),
                        ("shortcut.wav", token, url, 404),
                        ("../side A.wav", token, url, 404),
                    ]:
                        with self.assertRaises(HTTPError) as error:
                            download(name, key, origin)
                        self.assertEqual(error.exception.code, status)
                        error.exception.close()
                finally:
                    server.shutdown()
                    thread.join()

    def test_saved_side_preview_serves_authenticated_byte_ranges(self):
        with TemporaryDirectory() as directory:
            library = Path(directory)
            (library / "side.wav").write_bytes(b"RIFF-preview")
            with ThreadingHTTPServer(("127.0.0.1", 0), panel_handler) as server:
                server.RequestHandlerClass = panel_handler(
                    library / "control.sock", server.server_port, library
                )
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"http://127.0.0.1:{server.server_port}"
                    with urlopen(url + "/review/?side=side.wav", timeout=4) as response:
                        cookie = response.headers["Set-Cookie"].split(";")[0]
                    target = url + "/recordings/audio?side=side.wav"
                    with self.assertRaises(HTTPError) as error:
                        urlopen(target, timeout=4)
                    self.assertEqual(error.exception.code, 403)
                    error.exception.close()
                    request = Request(target, headers={"Cookie": cookie, "Range": "bytes=1-4"})
                    with urlopen(request, timeout=4) as response:
                        self.assertEqual(response.status, 206)
                        self.assertEqual(response.headers["Content-Range"], "bytes 1-4/12")
                        self.assertEqual(response.read(), b"IFF-")
                finally:
                    server.shutdown()
                    thread.join()
