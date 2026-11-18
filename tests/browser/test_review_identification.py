"""Exercise long saved-side review and mocked AcoustID results in real Chrome."""
import json
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
RATE = 48_000
SECONDS = 25 * 60
FRAMES = RATE * SECONDS
POINTS = [0, 244 * RATE, 360 * RATE]
FINGERPRINT_REQUESTS = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/recordings/audio"):
            total = 44 + FRAMES * 4
            header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", total - 8, b"WAVE",
                                 b"fmt ", 16, 1, 2, RATE, RATE * 4, 4, 16, b"data", FRAMES * 4)
            requested = self.headers.get("Range", "bytes=0-1048575").removeprefix("bytes=")
            start_text, _, end_text = requested.partition("-")
            start = int(start_text or 0)
            end = min(int(end_text) if end_text else start + 1_048_575,
                      total - 1, start + 1_048_575)
            content = bytearray()
            if start < 44:
                content.extend(header[start:min(44, end + 1)])
            if end >= 44:
                data_start = max(start, 44)
                content.extend(bytes(end - data_start + 1))
            self.send_response(206)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        path = ROOT / urlsplit(self.path).path.lstrip("/")
        if path.is_dir():
            path = path / "index.html"
        if not path.is_file():
            self.send_error(404)
            return
        content = path.read_bytes()
        content_type = "text/html; charset=utf-8" if path.suffix == ".html" else "text/javascript"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        if self.path == "/recordings/waveform":
            result = {"frames": FRAMES, "sample_rate": RATE, "peaks": [[0, 0]] * 1000,
                      "suggested_starts": [0, *POINTS[1:], 12 * 60 * RATE, 20 * 60 * RATE],
                      "matches": []}
        elif self.path == "/recordings/draft":
            result = {"frames": FRAMES, "sample_rate": RATE, "points": POINTS,
                      "tags": [], "confirmed": [], "selected": POINTS[-1], "format": "wav"}
        elif self.path == "/recordings/fingerprint":
            FINGERPRINT_REQUESTS.append(body)
            if len(FINGERPRINT_REQUESTS) == 1:
                result = {"duration_seconds": 1140, "sample_position": "middle",
                          "sample_start_seconds": 510, "sample_duration_seconds": 120,
                          "matches": [{"score": 0.91, "recordings": [{
                              "recording_mbid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                              "title": "Synthetic Song", "artist": "Synthetic Artist"}]}]}
                status = 200
            elif len(FINGERPRINT_REQUESTS) == 2:
                result = {"duration_seconds": 1140, "sample_position": "middle",
                          "sample_start_seconds": 510, "sample_duration_seconds": 120, "matches": []}
                status = 200
            else:
                result = {"error": "Synthetic provider failure"}
                status = 503
        else:
            result, status = {"error": "unknown test route"}, 404
        if self.path != "/recordings/fingerprint":
            status = 200
        encoded = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


with TemporaryDirectory() as temp:
    # Sparse local master: the test reads only its WAV header, never materializes 288 MB.
    master = Path(temp) / "marina-secusa.wav"
    master.write_bytes(struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + FRAMES * 4,
                                   b"WAVE", b"fmt ", 16, 1, 2, RATE, RATE * 4, 4,
                                   16, b"data", FRAMES * 4))
    with master.open("r+b") as output:
        output.truncate(44 + FRAMES * 4)
    FINGERPRINT_REQUESTS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=os.environ.get("CHROME_PATH"))
            page = browser.new_page(viewport={"width": 390, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/review/?side={master.name}")
            page.wait_for_function('document.querySelector("#load-progress").hidden && !document.querySelector("#track-editor").hidden')
            page.get_by_role("button", name="Seek track start at 360.000 seconds: Track 3").wait_for()
            page.get_by_role("button", name="Seek track start at 360.000 seconds: Track 3").click()
            assert "19 minutes long" in page.locator("#long-track-warning").inner_text()
            assert page.get_by_role("button", name="Add 2 suggested starts within this track").is_visible()
            page.locator("#track-editor details > summary").click()
            assert page.locator("#fingerprint-sample-wrap").is_visible()
            page.locator("#acoustid-consent").check()
            page.locator("#fingerprint-sample").select_option("middle")
            page.get_by_role("button", name="Identify selected track").click()
            page.get_by_text("Synthetic Song · Synthetic Artist · 91%").wait_for()
            assert FINGERPRINT_REQUESTS[0]["sample_position"] == "middle"
            page.get_by_role("button", name="Fill blank title and artist").click()
            assert page.locator("#track-title").input_value() == "Synthetic Song"
            page.locator("#track-title").fill("Manual correction")
            page.get_by_role("button", name="Fill blank title and artist").click()
            assert page.locator("#track-title").input_value() == "Manual correction"
            page.get_by_role("button", name="Identify selected track").click()
            assert len(FINGERPRINT_REQUESTS) == 1  # same interval is served from the session cache
            assert "Cached result; no new request sent" in page.locator("#fingerprint-result").inner_text()
            page.get_by_role("button", name="Recheck with AcoustID").click()
            page.get_by_text("No AcoustID match was found").wait_for()
            assert len(FINGERPRINT_REQUESTS) == 2
            page.locator("#fingerprint-sample").select_option("end")
            page.get_by_role("button", name="Identify selected track").click()
            page.get_by_text("AcoustID lookup failed: Synthetic provider failure").wait_for()
            assert FINGERPRINT_REQUESTS[2]["sample_position"] == "end"
            page.get_by_role("button", name="Add 2 suggested starts within this track").click()
            assert page.locator("#splits li").count() == 5
            assert "unconfirmed markers" in page.locator("#editor-status").inner_text()
            assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()

print("PASS: saved 19-minute track, bounded middle/end excerpts, cache/recheck, no-match/error, manual-tag safety, and split recovery")
