"""Optional real-browser smoke test. Uses its own temporary database and server.

Install playwright separately and run: python tests/browser_smoke.py
Pass --browser /usr/bin/chromium to use an existing system browser.
"""

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter with application dependencies",
    )
    parser.add_argument("--browser", default=None, help="Optional Chromium executable")
    parser.add_argument("--screenshots", default=None, help="Optional output directory")
    args = parser.parse_args()
    errors = []
    with tempfile.TemporaryDirectory() as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = subprocess.Popen(
            [
                args.python,
                "-m",
                "uvicorn",
                "backend.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "DOCUMENT_DB_PATH": str(Path(directory) / "test.sqlite3"),
            },
            stdout=subprocess.DEVNULL,
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(160):
                try:
                    opener.open(
                        f"http://127.0.0.1:{port}/api/health", timeout=1
                    ).close()
                    break
                except OSError:
                    if server.poll() is not None:
                        raise RuntimeError("Server exited during startup")
                    time.sleep(0.25)
            else:
                raise RuntimeError("Server did not become ready")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=args.browser, headless=True, args=["--no-sandbox"]
                )
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1100}, reduced_motion="reduce"
                )
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}")
                page.get_by_text("Workspace connected", exact=True).wait_for()
                assert page.locator("#ask-btn").is_disabled()
                page.locator("#sample-btn").click()
                page.wait_for_function(
                    "document.querySelector('#stat-docs').textContent === '1'"
                )
                assert "observatory" in page.locator("#question").input_value()
                page.locator("#ask-btn").click()
                page.locator(".turn").wait_for()
                assert "200" in page.locator(".answer-text").inner_text()
                page.locator(".citation").first.click()
                assert page.locator(".source").first.get_attribute("open") is not None
                with page.expect_download() as download:
                    page.locator("#export-btn").click()
                exported = Path(download.value.path()).read_text()
                assert "Sources" in exported and "200" in exported
                if args.screenshots:
                    output = Path(args.screenshots)
                    output.mkdir(parents=True, exist_ok=True)
                    page.locator("#toast").evaluate("node => node.hidden = true")
                    page.screenshot(path=str(output / "workspace.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(output / "mobile.png"), full_page=True)
                    page.set_viewport_size({"width": 1440, "height": 1100})
                # A filename and document containing HTML must remain inert text.
                malicious_name = "<img src=x onerror=alert(1)>.txt"
                page.locator("#file-input").set_input_files(
                    {
                        "name": malicious_name,
                        "mimeType": "text/plain",
                        "buffer": b"The telescope manual says <img src=x onerror=alert(1)>.",
                    }
                )
                page.wait_for_function(
                    "document.querySelector('#stat-docs').textContent === '2'"
                )
                assert (
                    page.locator(".doc-name").filter(has_text=malicious_name).count()
                    == 1
                )
                assert page.locator("#doc-list img").count() == 0
                # Upload again through the same input: regression for detached inputs.
                page.locator("#file-input").set_input_files(
                    {
                        "name": "second.md",
                        "mimeType": "text/plain",
                        "buffer": b"The telescope opens at noon in Paris.",
                    }
                )
                page.wait_for_function(
                    "document.querySelector('#stat-docs').textContent === '3'"
                )
                page.locator("#document-scope").select_option("second.md")
                page.locator("#question").fill("Where is the telescope?")
                page.locator("#question").press("Enter")
                page.wait_for_function(
                    "document.querySelectorAll('.turn').length === 2"
                )
                assert "Paris" in page.locator(".answer-text").last.inner_text()
                assert "Jaipur" not in page.locator(".answer-text").last.inner_text()
                page.locator("#file-search").fill("second")
                assert page.locator(".doc-item").count() == 1
                page.locator("#file-search").fill("")
                page.locator("#reset-btn").click()
                page.get_by_role("button", name="Cancel", exact=True).click()
                assert page.locator("#stat-docs").inner_text() == "3"
                # Reload reconstructs the SQLite-backed library, not the session.
                page.reload()
                page.wait_for_function(
                    "document.querySelector('#stat-docs').textContent === '3'"
                )
                assert page.locator(".turn").count() == 0
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                page.locator("#question").fill("When does the telescope open?")
                page.locator("#ask-btn").click()
                page.locator(".turn").wait_for()
                page.locator("#reset-btn").click()
                page.locator("#confirm-action").click()
                page.wait_for_function(
                    "document.querySelector('#stat-docs').textContent === '0'"
                )
                assert page.locator("#ask-btn").is_disabled()
                assert page.locator(".turn").count() == 0
                assert not errors, errors
                browser.close()
            print(
                "PASS: real browser sample, citations, export, HTML escaping, repeated upload, document scope, search, confirmation, reload, mobile layout, reset; no JavaScript errors"
            )
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    main()
