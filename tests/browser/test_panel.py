"""Run the control panel against generated PCM in isolated Chrome."""
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import wave

from playwright.sync_api import sync_playwright


project = Path(__file__).resolve().parents[2]
with TemporaryDirectory() as folder:
    root = Path(folder)
    control = root / "control.sock"
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    with subprocess.Popen(
        [sys.executable, project / "session_cli.py", "serve", control, root],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
    ) as relay:
        try:
            for _ in range(50):
                if control.exists():
                    break
                time.sleep(0.05)
            assert control.exists()
            with subprocess.Popen(
                [sys.executable, project / "panel.py", control, "--port", str(port)],
                stderr=subprocess.DEVNULL,
            ) as panel:
                try:
                    time.sleep(0.3)
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(executable_path=os.environ.get("CHROME_PATH"))
                        page = browser.new_page(viewport={"width": 390, "height": 800})
                        page.goto(f"http://127.0.0.1:{port}/")
                        page.get_by_text("Recording: idle").wait_for()
                        denied = page.request.post(f"http://127.0.0.1:{port}/control", data="{}")
                        assert denied.status == 403
                        page.get_by_role("button", name="Start recording").click()
                        page.get_by_text("Recording: recording").wait_for()
                        relay.stdin.write(b"abcd" * 4800)
                        relay.stdin.flush()
                        time.sleep(0.2)
                        page.get_by_role("button", name="Stop recording").click()
                        page.get_by_text("Saved 4800 frames.").wait_for()
                        with wave.open(str(root / "side-a.wav"), "rb") as audio:
                            assert audio.readframes(4800) == b"abcd" * 4800
                        page.get_by_role("checkbox").check()
                        page.get_by_text("Start the session with a configured recognition provider").wait_for()
                        assert not page.get_by_role("checkbox").is_checked()
                        browser.close()
                finally:
                    panel.terminate()
        finally:
            relay.stdin.close()
            relay.wait(timeout=10)
print("PASS: panel recording, generated audio parity, and rejected unconfigured/unauthenticated controls")
