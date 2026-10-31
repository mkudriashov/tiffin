"""Capture the disposable demo at phone and wide viewport sizes."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "docs" / "images"
SIZES = {"phone": (390, 844), "wide": (1280, 800)}
PAGES = ("today", "menu", "batch", "dishes", "choose", "shopping", "progress", "more")


def free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def wait_for_demo(port: int, process: subprocess.Popen[bytes]) -> None:
    url = f"http://127.0.0.1:{port}/api/health"
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("tiffin demo exited before it started")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError, URLError:
            time.sleep(0.1)
    raise RuntimeError("tiffin demo did not become ready within ten seconds")


def main() -> int:
    try:
        from playwright.sync_api import Error as PlaywrightError  # noqa: PLC0415
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError:
        print("Screenshots skipped: install Playwright and its Chromium browser first.")
        return 0

    IMAGES.mkdir(parents=True, exist_ok=True)
    transcript = IMAGES / "telegram-transcript.html"
    subprocess.run(
        [sys.executable, "-m", "tiffin", "demo", "--telegram-transcript", str(transcript)],
        cwd=ROOT,
        check=True,
    )
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "tiffin", "demo", "--port", str(port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        wait_for_demo(port, process)
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except PlaywrightError as exc:
                if "Executable doesn't exist" not in str(exc):
                    raise
                print("Screenshots skipped: install Playwright's Chromium browser first.")
                return 0
            try:
                for size, (width, height) in SIZES.items():
                    page = browser.new_page(
                        viewport={"width": width, "height": height}, device_scale_factor=1
                    )
                    page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
                    page.locator("#today-rings > *").first.wait_for(timeout=15_000)
                    for name in PAGES:
                        if name in ("batch", "dishes", "choose"):
                            page.locator("#tab-menu").click()
                            button = page.locator(f'#menu-views button[data-view="{name}"]')
                            if not button.is_visible():
                                continue
                            button.click()
                        else:
                            page.locator(f"#tab-{name}").click()
                        page.wait_for_timeout(350)
                        page.screenshot(path=str(IMAGES / f"{name}-{size}.png"), full_page=True)
                        if name == "progress":
                            for detail_name, element_id in (
                                ("summary", "progress-sunday"),
                                ("measurements", "progress-measures"),
                                ("trends", "progress-explore"),
                            ):
                                page.locator(f"#{element_id} summary").click()
                                page.wait_for_timeout(350)
                                page.screenshot(
                                    path=str(IMAGES / f"progress-{detail_name}-{size}.png"),
                                    full_page=True,
                                )
                                page.locator(f"#{element_id} summary").click()
                    transcript_page = browser.new_page(viewport={"width": width, "height": height})
                    transcript_page.goto(transcript.resolve().as_uri(), wait_until="load")
                    telegram_name = "telegram.png" if size == "phone" else "telegram-wide.png"
                    transcript_page.screenshot(path=str(IMAGES / telegram_name), full_page=True)
                    transcript_page.close()
                    page.close()
            finally:
                browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    print(f"Screenshots saved in {IMAGES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
