"""Prove customer Streamlit pages work when the web process has no database."""

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
    parser.add_argument("--url", default="http://127.0.0.1:63045")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 1050})
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(args.url, wait_until="networkidle")
        page.get_by_text("Local Streamlit manager", exact=True).wait_for()

        if page.get_by_text("Welcome to Credent", exact=True).count():
            page.get_by_text("Continue an existing organization", exact=True).click()
            organization = page.get_by_role(
                "button", name=re.compile(r"^Workflow test\s*·\s*1 site$")
            )
            organization.wait_for()
            organization.click()
        page.get_by_role("button", name="Organization settings").wait_for()

        destinations = (
            ("Organization settings", "Organization settings"),
            ("Sales data", "Sales data"),
            ("Events", "Events workspace"),
            ("Marketing", "Marketing"),
        )
        for button_name, heading in destinations:
            page.get_by_role("button", name=button_name, exact=True).click()
            page.get_by_text(heading, exact=True).last.wait_for()
            alerts = page.locator("[data-testid='stAlert']").all_text_contents()
            unavailable = [
                item for item in alerts
                if "unavailable" in item.casefold() or "couldn’t" in item.casefold()
            ]
            if unavailable:
                raise AssertionError({"page": heading, "alerts": unavailable})
            page.get_by_role("button", name="Back to Site / Unit", exact=True).click()
            page.get_by_role("button", name=button_name, exact=True).wait_for()

        if page_errors:
            raise AssertionError({"page_errors": page_errors})
        print("gateway-only Streamlit smoke test passed")
        browser.close()


if __name__ == "__main__":
    main()
