"""Developer smoke test for local identity and organization administration."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re

LOCAL_BROWSERS = Path(__file__).resolve().parents[1] / ".playwright-browsers"
if LOCAL_BROWSERS.exists():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(LOCAL_BROWSERS))

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:63044")
    args = parser.parse_args()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(args.url, wait_until="networkidle")

        page.get_by_text("Local Streamlit manager", exact=True).wait_for()
        if page.get_by_text("Welcome to Credent", exact=True).count():
            page.get_by_text("Continue an existing organization", exact=True).click()
            page.get_by_role("button", name="Workflow test · 1 site").click()
            page.get_by_role("button", name="Organization settings").wait_for()
        settings_button = page.get_by_role("button", name="Organization settings")
        try:
            settings_button.click()
        except Exception:
            print(page.locator("body").inner_text(), flush=True)
            page.screenshot(path=".visual-tests/identity-admin-failure.png", full_page=True)
            raise
        page.get_by_text("Organization settings", exact=True).last.wait_for()
        page.get_by_role("tab", name="Administrators").click()
        page.get_by_text("Local Streamlit manager", exact=True).last.wait_for()
        page.get_by_role("tab", name="Ad channels").click()
        page.get_by_text(re.compile(r" channels$", re.IGNORECASE)).first.wait_for()

        alerts = page.locator("[data-testid='stAlert']").all_text_contents()
        errors = [alert for alert in alerts if "error" in alert.casefold()]
        if page_errors or errors:
            raise SystemExit({"page_errors": page_errors, "alerts": errors})
        print("identity and organization administration smoke test passed")
        browser.close()


if __name__ == "__main__":
    main()
