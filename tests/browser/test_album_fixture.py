"""Exercise album creation, capture, restart history, and reflow in a browser.

Capture is mocked with a generated WAV; the test never opens a physical device or
contacts an online metadata service.
"""
import json
import base64
import math
import os
from pathlib import Path
import socket
import tempfile
from threading import Thread
import wave
from http.server import ThreadingHTTPServer

from playwright.sync_api import sync_playwright

from album_service import AlbumService
from catalog import Catalog
import panel
from panel import panel_handler


project_root = Path(__file__).resolve().parents[2]
RELEASE_IDS = ("00000000-0000-4000-8000-000000000010", "00000000-0000-4000-8000-000000000011")
TINY_JPEG = base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIQAxAAAAH/2Q==")


class FixtureReleaseCatalog:
    def __init__(self):
        self.revision = 0
        self.release = None
        self.cover = None

    def search(self, **query):
        return [{"release_mbid": RELEASE_IDS[index],
                 "title": "Synthetic Album" if index == 0 else "Synthetic Album · alternate pressing",
                 "artist": "Synthetic Artist", "date": "1984", "country": "GB",
                 "formats": ["Vinyl"], "labels": [{"name": "Fixture Label", "catalog_number": f"FX-{index}"}],
                 "track_count": 10, "matched_tracks": [{"title": "Synthetic Track", "score": 98-index}]}
                for index in range(2)]

    def fetch_release(self, release_mbid):
        index = RELEASE_IDS.index(release_mbid)
        return {"release_mbid": release_mbid, "title": "Synthetic Album" if index == 0 else "Synthetic Album · alternate pressing",
                "artist": "Synthetic Artist", "date": "1984", "country": "GB", "labels": [],
                "media": [{"position": 1, "format": "Vinyl", "tracks": [
                    {"position": 1, "number": "A1", "title": "Synthetic Track", "artists": ["Synthetic Artist"]}]}]}

    def cover_art_candidates(self, release_mbid):
        return [{"image_id": str(index), "comment": f"Front {index}",
                 "thumbnail_url": f"/api/releases/artwork?release_mbid={release_mbid}&image_id={index}"}
                for index in (1, 2)]

    def cover_thumbnail(self, release_mbid, image_id):
        if image_id not in {item["image_id"] for item in self.cover_art_candidates(release_mbid)}:
            raise ValueError("Invalid fixture cover")
        return TINY_JPEG

    def pin(self, project_id, release_mbid, *, expected_revision, cover_image_id=None):
        assert expected_revision == self.revision
        self.revision += 1
        self.release = self.fetch_release(release_mbid)
        self.cover = cover_image_id
        return {"revision": self.revision, "release": self.release}

    def get_pin(self, project_id):
        return {"revision": self.revision, "release_mbid": self.release["release_mbid"] if self.release else None,
                "release": self.release,
                "cover": ({"image_id": self.cover, "thumbnail_url":
                           f"/api/releases/artwork?release_mbid={self.release['release_mbid']}&image_id={self.cover}"}
                          if self.release and self.cover else None)}
with tempfile.TemporaryDirectory(prefix="record-relay-browser-") as folder:
    root = Path(folder)
    library = root / "library"
    library.mkdir()
    catalog = Catalog(root / "state" / "catalog.sqlite", library)
    active_id = None
    active_side = None
    live_view_clients = set()
    audio_window = bytes(max(0, min(255, round(128
                         + 36 * math.sin(2 * math.pi * 7 * i / 1024)
                         + 28 * math.sin(2 * math.pi * 24 * i / 1024)
                         + 20 * math.sin(2 * math.pi * 61 * i / 1024)
                         + 12 * math.sin(2 * math.pi * 133 * i / 1024))))
                         for i in range(1024))

    def fake_control(_path, packet):
        global active_id, active_side
        command = json.loads(packet)
        if command["action"] == "status":
            client_id = command.get("visual_client_id")
            return json.dumps({"capture_generation": "synthetic-capture", "boot_generation": "synthetic-boot",
                               "recording": "recording" if active_id else "idle",
                               "recording_id": active_id, "recording_side": active_side,
                               "track": ({"title": "Synthetic Track", "artist": "Synthetic Artist",
                                          "album": "Synthetic Album"} if active_id else None),
                               "signal": True, "buffered_seconds": 1,
                               "buffered_oldest_frame": 0, "buffered_newest_frame": 96000,
                               "side_a_end_frame": 48000,
                               "side_a_end_capture_generation": "synthetic-capture",
                               "live_view": bool(live_view_clients),
                               "live_view_client": client_id in live_view_clients,
                               "live_view_clients": len(live_view_clients), "visual_sequence": 1,
                               "visual_age_seconds": 0.1,
                               "visual_samples": [0.25] * 64 if live_view_clients else None,
                               "visual_audio": ({"timeByteArray": base64.b64encode(audio_window).decode(),
                                                "timeByteArrayL": base64.b64encode(audio_window).decode(),
                                                "timeByteArrayR": base64.b64encode(audio_window).decode()}
                                               if live_view_clients else None),
                               "visual_dropped_frames": 0,
                               "offline_queue": False, "listen_queue": [],
                               "identify": False, "scrobble": False}).encode()
        if command["action"] == "visual_client":
            if command["enabled"]:
                live_view_clients.add(command["visual_client_id"])
            else:
                live_view_clients.discard(command["visual_client_id"])
            return fake_control(_path, json.dumps({"action": "status",
                                                   "visual_client_id": command["visual_client_id"]}).encode())
        if command["action"] in ("record", "save_buffer"):
            active_id = command["request_id"]
            active_side = command["side"]
            with wave.open(str(library / command["name"]), "wb") as writer:
                writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                writer.writeframes(b"\x24\x00\x24\x00" * 4800)
            return b"{}"
        if command["action"] == "stop":
            boundary = 48000 if active_side == "A" else None
            active_id = None
            active_side = None
            return json.dumps({"recorded_frames":4800, "side_a_end_frame": boundary,
                               "side_a_end_capture_generation": "synthetic-capture"}).encode()
        raise AssertionError(command)

    service = AlbumService(catalog, root / "capture.sock", releases=FixtureReleaseCatalog(), send=fake_control)
    # Panel control actions travel through this module-level socket adapter;
    # point it at the same synthetic session used by AlbumService.
    panel.forward = fake_control
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), panel_handler(
        root / "capture.sock", port, library, album_service=service))
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=os.environ.get("CHROME_PATH"))
            context = browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="no-preference")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            base = f"http://127.0.0.1:{port}/album/"
            page.goto(base)
            page.get_by_role("heading", name="Choose an album").wait_for()
            page.once("dialog", lambda dialog: dialog.accept("Synthetic Album"))
            page.get_by_role("button", name="Start a different album").click()
            page.get_by_role("heading", name="Synthetic Album").wait_for()
            assert not page.locator("#auto-album-suggestions").is_checked()
            page.get_by_role("button", name="Record from now").click()
            page.get_by_role("button", name="Stop and save side").wait_for()
            page.locator("#auto-album-suggestions").check()
            page.get_by_role("radio", name="Synthetic Album · Synthetic Artist · 1984 · GB · Vinyl · Fixture Label · FX-0 · 10 tracks · matched track: Synthetic Track (score 98)").wait_for()
            assert page.locator("#live-release-candidates .candidate").count() == 2
            assert "unconfirmed" in page.locator("#live-suggestion-status").inner_text().lower()
            page.locator("#live-release-candidates input[type=radio]").nth(1).check()
            page.get_by_text("Synthetic Album · alternate pressing · Synthetic Artist", exact=True).wait_for()
            page.get_by_role("radio", name="Cover option 1 · Front 1").check()
            assert page.locator("#pin-release").is_enabled()
            assert page.locator("#release-candidates .candidate").count() == 0
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Pin selected edition").click()
            page.get_by_text("Release pinned to this album project.").wait_for()
            page.locator("#pinned-cover").wait_for(state="visible")
            page.wait_for_function("document.querySelector('#pinned-cover').naturalWidth === 1")
            page.locator("#search-album").fill("Manual Album")
            page.locator("#search-artist").fill("Manual Artist")
            page.get_by_role("button", name="Search MusicBrainz").click()
            page.locator("#release-candidates .candidate").nth(1).wait_for()
            assert page.locator("#live-release-candidates .candidate").count() == 2
            assert page.locator("#release-candidates .candidate").count() == 2
            page.get_by_role("button", name="Stop and save side").click()
            page.get_by_text("Side saved").wait_for()
            page.reload()
            page.get_by_text("Side A · Saved · 0.1 sec", exact=False).wait_for()
            page.get_by_role("button", name="Side B").click()
            assert page.locator("#side-heading").inner_text() == "Side B"
            page.locator("#side-b-start").wait_for(state="visible")
            page.locator("#live-view").check()
            page.wait_for_timeout(100)
            page.locator("#live-canvas").wait_for(state="visible")
            assert "Latest waveform frame" in page.locator("#live-view-status").inner_text()
            page.locator("#visual-mode").select_option("spectrum")
            spectrum_pixels = page.locator("#live-canvas").evaluate("""canvas => {
              const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
              let count = 0; for (let i = 0; i < data.length; i += 4) {
                if (data[i + 1] > 80 && data[i + 2] > 60) count++;
              }
              return count;
            }""")
            assert spectrum_pixels > 10, spectrum_pixels
            page.locator("#visual-delay").evaluate("el => { el.value = '500'; el.dispatchEvent(new Event('input', {bubbles: true})); }")
            assert page.locator("#visual-delay-value").evaluate("output => output.value") == "500 ms"
            page.get_by_role("button", name="Open animated visualizer").click()
            dialog = page.get_by_role("dialog", name="Live visualizer")
            dialog.wait_for()
            dialog_accessibility = dialog.aria_snapshot()
            assert "Live visualizer" in dialog_accessibility
            assert "Preset" in dialog_accessibility
            assert "Close visualizer" in dialog_accessibility
            assert page.get_by_role("combobox", name="Preset").is_visible()
            assert page.get_by_role("button", name="Close visualizer").is_visible()
            assert page.locator("#live-canvas").get_attribute("aria-label") == "Live input frequency spectrum."
            assert page.locator("#visualizer-canvas").get_attribute("aria-describedby") == "visual-summary"
            assert "Approximate input spectrum" in page.locator("#visual-summary").inner_text()
            page.locator("#visualizer-preset").select_option("radial")
            page.wait_for_timeout(150)
            page.screenshot(path="/tmp/record-relay-visualizer.png")
            painted_pixels = page.locator("#visualizer-canvas").evaluate("""canvas => {
              const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
              let count = 0; for (let i = 0; i < data.length; i += 4) {
                if (data[i] > 24 || data[i + 1] > 24 || data[i + 2] > 24) count++;
              }
              return count;
            }""")
            assert painted_pixels > 100, painted_pixels
            radial_signature = page.locator("#visualizer-canvas").evaluate("""canvas => {
              const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
              let hash = 0; for (let i = 0; i < data.length; i += 97) hash = (hash * 31 + data[i]) >>> 0;
              return hash;
            }""")
            page.locator("#visualizer-preset").select_option("ribbon")
            page.wait_for_timeout(100)
            page.screenshot(path="/tmp/record-relay-visualizer-ribbon.png")
            ribbon_signature = page.locator("#visualizer-canvas").evaluate("""canvas => {
              const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
              let hash = 0; for (let i = 0; i < data.length; i += 97) hash = (hash * 31 + data[i]) >>> 0;
              return hash;
            }""")
            assert radial_signature != ribbon_signature
            page.keyboard.press("Escape")
            page.get_by_role("dialog").wait_for(state="hidden")
            assert page.evaluate("document.documentElement.scrollWidth === innerWidth"), page.evaluate("""() => Array.from(document.querySelectorAll('body *')).filter(el => el.getBoundingClientRect().right > innerWidth + 1).map(el => ({tag: el.tagName, id: el.id, cls: el.className, right: el.getBoundingClientRect().right, width: el.getBoundingClientRect().width, text: el.innerText?.slice(0, 80)}))""")
            page.set_viewport_size({"width": 320, "height": 720})
            assert page.evaluate("document.documentElement.scrollWidth === innerWidth"), page.evaluate("""() => Array.from(document.querySelectorAll('body *')).filter(el => el.getBoundingClientRect().right > innerWidth + 1).map(el => ({tag: el.tagName, id: el.id, cls: el.className, right: el.getBoundingClientRect().right, width: el.getBoundingClientRect().width, text: el.innerText?.slice(0, 80)}))""")
            page.screenshot(path="/tmp/record-relay-mobile.png", full_page=True)
            page.set_viewport_size({"width": 1280, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
            page.screenshot(path="/tmp/record-relay-desktop.png", full_page=True)
            page.emulate_media(reduced_motion="reduce")
            page.wait_for_function("document.querySelector('#open-visualizer').disabled && document.querySelector('#live-view').checked && !document.querySelector('#live-canvas').hidden && document.querySelector('#visual-mode').value === 'spectrum' && document.querySelector('#visual-mode option[value=waveform]').disabled")
            assert page.locator("#live-canvas").is_visible()
            page.locator("#live-view").uncheck()
            page.wait_for_function("!document.querySelector('#live-view').checked && document.querySelector('#live-canvas').hidden && document.querySelector('#open-visualizer').disabled")
            assert not errors, errors
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
print("PASS: persistent album workbench, mocked save/reload, waveform and spectrum, visualizer accessibility, reduced motion, and 320/390/1280 px reflow")
