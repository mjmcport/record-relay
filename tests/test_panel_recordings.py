from http.server import ThreadingHTTPServer
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from panel import panel_handler


class RecordingDownloads(unittest.TestCase):
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
