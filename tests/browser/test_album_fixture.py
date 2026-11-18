"""Exercise the fixture-driven album workbench in a real browser."""
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

from playwright.sync_api import sync_playwright


project = Path(__file__).resolve().parents[2]
server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(project)))
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("CHROME_PATH"))
        for width in (390, 1280):
            page = browser.new_page(viewport={"width": width, "height": 850})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/ui/album.html")
            page.get_by_text("Recording side B · 0:03").wait_for()
            assert "track performer" in page.locator("#now-artist").inner_text()
            page.get_by_role("button", name="Side A").click()
            assert "Side A · saved" in page.locator("#side-heading").inner_text()
            page.get_by_role("button", name="Correct album or edition").click()
            page.locator("#album-edit").fill("Corrected Edition")
            page.get_by_role("button", name="Apply preview correction").click()
            assert page.locator("#album-title-heading").inner_text() == "Corrected Edition"
            page.get_by_role("button", name="Save recent audio…").click()
            assert "No recording action was sent" in page.locator("#side-status").inner_text()
            assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
            assert not errors, errors
            page.close()
            print(f"PASS: fixture album, side identity, correction preview and responsive layout at {width}px")
        browser.close()
finally:
    server.shutdown()
    server.server_close()
