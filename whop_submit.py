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


def _prefix(title: str) -> str:
    """The stable part of a campaign title: brands rename campaigns ("Clip Ali Choucair – Real Estate
    Content" became "Clip Ali Choucair – 21 y/o, 51 Rentals"), but the part before the dash stays."""
    import re

    return re.split(r"\s[–—|-]\s|\s\|", title, maxsplit=1)[0].strip()


def _click_through(page, frame, matches) -> bool:
    for i in range(min(matches.count(), 6)):
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


def _open_campaign(page, frame, title: str):
    """Exact title on the marketplace first; otherwise search Whop for the title's stable prefix.

    Returns the app frame showing the campaign (the search reloads the page, so the old frame is gone),
    or None."""
    if _click_through(page, frame, frame.get_by_text(title, exact=True)):
        return frame
    prefix = _prefix(title)
    print(f"  campaign title changed? searching Whop for {prefix!r}")
    page.goto(MARKETPLACE, wait_until="domcontentloaded", timeout=90000)
    frame = _app_frame(page)
    page.wait_for_timeout(3000)
    box = frame.locator("input[placeholder*='campaigns' i]").first
    box.click(force=True)
    box.fill(prefix)
    box.press("Enter")
    page.wait_for_timeout(9000)
    return frame if _click_through(page, frame, frame.get_by_text(prefix, exact=False)) else None


CHECKS = ("Posted from one of your linked accounts", "Not already submitted to this campaign",
          "Posted within the last 30 minutes")


class SubmissionRejected(RuntimeError):
    def __init__(self, failed: list[str]):
        self.failed = failed
        super().__init__(f"Whop rejected the link: {', '.join(failed)}")

    @property
    def retryable(self) -> bool:
        # a fresh post often can't be fetched yet, so the ownership check fails at first
        return all("linked accounts" in f for f in self.failed)


def submit_with_retry(clip: dict, url: str, target: dict, window_minutes: int = 28) -> bool:
    """Retries until shortly before Whop's 30-minute window closes.

    Anything short of a clear answer is retried: slow page loads, a submission whose confirmation didn't
    show in time (the retry then sees "already submitted" and counts it as done), or the ownership
    check failing because the platform hasn't published the post yet."""
    import time as _t

    waits = [0, 90, 180, 300, 420, 540]  # seconds between attempts, ~25 min in total
    start = _t.time()
    last: Exception | None = None
    for i, w in enumerate(waits):
        if _t.time() - start + w > window_minutes * 60:
            break
        if w:
            if i == 1:  # the first attempt failed: tell you now, while there's time to do it by hand
                try:
                    from notify import alert

                    alert(f"⏳ Whop auto-submit is struggling: {target['label']}",
                          f"Campaign: {clip['campaign_title']}\nLink: {url}\n\nStill retrying for up to ~25 min. "
                          "If you can, submit it by hand now (Whop → Content Rewards → campaign → Submit clip).")
                except Exception:
                    pass
            print(f"  retrying Whop submission in {w}s (attempt {i + 1})")
            _t.sleep(w)
        try:
            if submit(clip, url, target):
                return True
            last = RuntimeError("no confirmation from Whop after clicking Submit")
            print("  no confirmation yet - checking again (a repeat shows 'already submitted' if it went through)")
        except SubmissionRejected as err:
            last = err
            print(f"  {err}")
            if not err.retryable:
                raise
        except Exception as err:  # page didn't load, campaign not found, selector timeout...
            last = err
            print(f"  attempt failed: {err.__class__.__name__}: {str(err)[:160]}")
    raise last or RuntimeError("Whop submission did not succeed inside the window")


def _failed_checks(dlg) -> list[str]:
    """Names of checks whose label Whop has turned red."""
    failed = []
    for name in CHECKS:
        el = dlg.get_by_text(name, exact=False).first
        try:
            rgb = el.evaluate("e => getComputedStyle(e).color")  # e.g. 'rgb(239, 68, 68)'
            r, g, b = [int(x) for x in rgb[rgb.index("(") + 1:rgb.index(")")].split(",")[:3]]
            if r > 180 and g < 120 and b < 120:
                failed.append(name)
        except Exception:
            continue
    return failed


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
            if os.environ.get("WHOP_SESSION_OUT"):  # keep Whop's rotated cookies; renew.py stores them
                ctx.storage_state(path=os.environ["WHOP_SESSION_OUT"])
            _scroll(frame)
            frame = _open_campaign(page, frame, clip["campaign_title"])
            if frame is None:
                page.screenshot(path=str(artifacts / f"{clip['clip_id']}-no-campaign.png"))
                raise RuntimeError(f"could not open campaign {clip['campaign_title']!r}")

            frame.locator("button:has-text('Submit clip')").first.click(timeout=20000)
            page.wait_for_timeout(3000)
            dlg = frame.locator("[role=dialog]").last
            box = dlg.locator("input[type=url], input[type=text], input[placeholder*='link' i], "
                              "input[placeholder*='http' i]").first
            box.fill(url)
            box.press("Tab")  # blur triggers Whop's checks: linked account / not submitted / < 30 min
            page.wait_for_timeout(4000)

            # "I've read the requirements..." is a custom control, not a native checkbox: click its label.
            consent = dlg.get_by_text("I've read the requirements", exact=False).first
            consent.click(timeout=10000)
            page.wait_for_timeout(1500)

            submit_btn = dlg.locator("button:has-text('Submit clip')").last
            page.wait_for_timeout(1500)
            page.screenshot(path=str(artifacts / f"{clip['clip_id']}-before-submit.png"))
            # Whop's checks stay grey unless one FAILS (label turns red). The faded button is not
            # really disabled in the DOM, so read the checks instead of trusting is_enabled().
            failed = _failed_checks(dlg)
            if any("already submitted" in f.lower() for f in failed):
                print("  whop says this link was already submitted - treating as done")
                return True
            if failed:
                raise SubmissionRejected(failed)
            submit_btn.click(timeout=20000)
            # success dialog: "Clip submitted - Your clip got submitted and is now part of the campaign."
            # Whop can sit on "Submitting..." for a while, so wait up to a minute for the answer.
            ok = False
            for _ in range(30):
                page.wait_for_timeout(2000)
                body = frame.inner_text("body").lower()
                if any(w in body for w in ("clip submitted", "now part of the campaign", "under review",
                                           "pending review", "in review")):
                    ok = True
                    break
                failed = _failed_checks(dlg) if dlg.count() else []
                if any("already submitted" in f.lower() for f in failed):
                    ok = True
                    break
                if failed:
                    page.screenshot(path=str(artifacts / f"{clip['clip_id']}-after-submit.png"))
                    raise SubmissionRejected(failed)
            page.screenshot(path=str(artifacts / f"{clip['clip_id']}-after-submit.png"))
            return ok
        finally:
            ctx.close()
            browser.close()
