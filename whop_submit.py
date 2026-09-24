"""Submit a posted clip on Whop, headless, using a stored login session.

Whop only accepts a submission within ~30 minutes of posting, so this runs immediately after a post
and fails loudly: an unsubmitted post earns nothing.

The campaign page cannot be opened by URL on its own (it 404s into a marketing page); it has to be
reached through the Content Rewards app shell, the same way a person would.
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


def _scroll(frame, rounds: int = 6) -> None:
    for _ in range(rounds):
        frame.evaluate("window.scrollBy(0, 2500)")
        frame.page.wait_for_timeout(600)


def _open_campaign(page, frame, title: str) -> bool:
    """Campaign cards open a full page in the member layout; try every copy of the title."""
    matches = frame.get_by_text(title, exact=True)
    for i in range(matches.count()):
        for how in ("real", "dom"):
            try:
                if how == "real":
                    matches.nth(i).click(force=True, timeout=5000)
                else:
                    matches.nth(i).evaluate("e => (e.closest('a,button,[role=button]') || e).click()")
            except Exception:
                continue
            for _ in range(8):
                page.wait_for_timeout(750)
                if frame.locator("button:has-text('Submit clip')").count():
                    return True
    return False


def submit(clip: dict, url: str, target: dict, artifacts: Path | None = None) -> bool:
    from playwright.sync_api import sync_playwright

    session = os.environ.get("WHOP_SESSION")
    if not session:
        raise RuntimeError("missing secret WHOP_SESSION")
    artifacts = artifacts or Path("artifacts")
    artifacts.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=json.loads(session), viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(MARKETPLACE, wait_until="domcontentloaded", timeout=90000)
            frame = _app_frame(page)
            page.wait_for_timeout(3000)
            if "/login" in page.url:
                raise RuntimeError("Whop session is stale - refresh WHOP_SESSION locally")
            _scroll(frame)
            if not _open_campaign(page, frame, clip["campaign_title"]):
                page.screenshot(path=str(artifacts / f"{clip['clip_id']}-no-campaign.png"))
                raise RuntimeError(f"could not open campaign {clip['campaign_title']!r}")

            frame.locator("button:has-text('Submit clip')").first.click(timeout=20000)
            page.wait_for_timeout(3000)
            dlg = frame.locator("[role=dialog]").last
            box = dlg.locator("input[type=url], input[type=text], input[placeholder*='link' i], "
                              "input[placeholder*='http' i]").first
            box.fill(url)
            page.wait_for_timeout(800)
            # "I've read the requirements and accept that non-compliant submissions may be auto-rejected."
            checks = dlg.locator("input[type=checkbox]")
            for i in range(checks.count()):
                try:
                    checks.nth(i).check(timeout=5000)
                except Exception:
                    pass
            page.screenshot(path=str(artifacts / f"{clip['clip_id']}-before-submit.png"))
            dlg.locator("button:has-text('Submit clip')").last.click(timeout=20000)
            page.wait_for_timeout(8000)
            page.screenshot(path=str(artifacts / f"{clip['clip_id']}-after-submit.png"))
            body = frame.inner_text("body").lower()
            return any(w in body for w in ("submitted", "under review", "pending review", "in review", "thanks"))
        finally:
            ctx.close()
            browser.close()
