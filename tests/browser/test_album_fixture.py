"""Exercise album creation, capture, restart history, and reflow in a browser.

Capture is mocked with a generated WAV; the test never opens a physical device or
contacts an online metadata service.
"""
import json
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
from panel import panel_handler


project_root = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix="record-relay-browser-") as folder:
    root = Path(folder)
    library = root / "library"
    library.mkdir()
    catalog = Catalog(root / "state" / "catalog.sqlite", library)
    active_id = None

    def fake_control(_path, packet):
        global active_id
        command = json.loads(packet)
        if command["action"] == "status":
            return json.dumps({"capture_generation": "synthetic-capture", "boot_generation": "synthetic-boot",
                               "recording": "recording" if active_id else "idle",
                               "recording_id": active_id, "signal": True, "buffered_seconds": 1,
                               "identify": False, "scrobble": False}).encode()
        if command["action"] in ("record", "save_buffer"):
            active_id = command["request_id"]
            with wave.open(str(library / command["name"]), "wb") as writer:
                writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                writer.writeframes(b"\x24\x00\x24\x00" * 4800)
            return b"{}"
        if command["action"] == "stop":
            active_id = None
            return b'{"recorded_frames":4800}'
        raise AssertionError(command)

    service = AlbumService(catalog, root / "capture.sock", send=fake_control)
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
            context = browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            base = f"http://127.0.0.1:{port}/album/"
            page.goto(base)
            page.get_by_role("heading", name="Choose an album").wait_for()
            page.once("dialog", lambda dialog: dialog.accept("Synthetic Album"))
            page.get_by_role("button", name="Start a different album").click()
            page.get_by_role("heading", name="Synthetic Album").wait_for()
            page.get_by_role("button", name="Record from now").click()
            page.get_by_role("button", name="Stop and save side").wait_for()
            page.get_by_role("button", name="Stop and save side").click()
            page.get_by_text("Side saved").wait_for()
            page.reload()
            page.get_by_text("Side A · Saved · 0.1 sec", exact=False).wait_for()
            page.get_by_role("button", name="Side B").click()
            assert page.locator("#side-heading").inner_text() == "Side B"
            assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
            page.set_viewport_size({"width": 1280, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
            assert not errors, errors
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
print("PASS: persistent album workbench, mocked save, reload history, and 390/1280 px reflow")
