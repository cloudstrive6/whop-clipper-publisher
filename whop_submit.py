"""Submit a posted clip on Whop, headless, using a stored login session.

The session comes from the WHOP_SESSION secret (Playwright storage_state JSON), exported locally
with `python -m clipper export-session`. Sessions expire; a failure here is loud on purpose,
because an unsubmitted post earns nothing.
"""
import json
import os
import time
from pathlib import Path

MARKETPLACE = "https://whop.com/discover/app/app_QRxsQodZgK1r4D/"


def _app_frame(page, timeout: float = 45):
    end = time.time() + timeout
    while time.time() < end:
        for f in page.frames:
            if ".apps.whop.com" in f.url:
                try:
                    if len(f.inner_text("body", timeout=2000)) > 200:
                        return f
                except Exception:
                    pass
        page.wait_for_timeout(1000)
    raise TimeoutError("Whop Content Rewards app did not load (session may be stale)")


def submit(clip: dict, url: str, target: dict, artifacts: Path | None = None) -> bool:
    from playwright.sync_api import sync_playwright

    session = os.environ.get("WHOP_SESSION")
    if not session:
        raise RuntimeError("missing secret WHOP_SESSION")
    artifacts = artifacts or Path("artifacts")
    artifacts.mkdir(exist_ok=True)
    state = json.loads(session)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=state, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        try:
            campaign_url = clip.get("campaign_url") or MARKETPLACE
            page.goto(campaign_url, wait_until="domcontentloaded", timeout=90000)
            frame = _app_frame(page)
            page.wait_for_timeout(3000)
            if "/login" in page.url or frame.get_by_text("Log in", exact=True).count():
                raise RuntimeError("Whop session is stale - refresh WHOP_SESSION locally")

            frame.locator("button:has-text('Submit clip'), button:has-text('Submit')").first.click(timeout=20000)
            page.wait_for_timeout(2500)
            box = frame.locator("input[type=url], input[placeholder*='http'], input[placeholder*='link' i]").first
            box.fill(url)
            files = frame.locator("input[type=file]")
            if files.count():
                files.first.set_input_files(str(clip["file"]))
                page.wait_for_timeout(4000)
            page.screenshot(path=str(artifacts / f"{clip['clip_id']}-before-submit.png"))
            frame.get_by_role("button", name="Submit").last.click()
            page.wait_for_timeout(6000)
            page.screenshot(path=str(artifacts / f"{clip['clip_id']}-after-submit.png"))
            body = frame.inner_text("body").lower()
            ok = any(w in body for w in ("submitted", "under review", "pending review", "thanks"))
            return ok
        finally:
            ctx.close()
            browser.close()
