"""Whop browser automation (Playwright, dedicated persistent profile).

Reads pages as visible text and lets Claude extract structure, so small UI changes don't break it.
Anything that sends data to Whop (join, apply, submit) stops for a human "y" in the terminal.
"""
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, Field

from . import db, llm
from .config import DATA, cfg, path
from .source import slug


@contextmanager
def browser(headless: bool = False):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        session = os.environ.get("WHOP_SESSION_FILE")
        if session:  # cloud: the saved login, written back afterwards so Whop's rotated cookies are kept
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(storage_state=session, viewport={"width": 1440, "height": 900})
            try:
                yield ctx.new_page()
                ctx.storage_state(path=session)
            finally:
                b.close()
            return
        ctx = p.chromium.launch_persistent_context(
            str(path(cfg()["whop"]["browser_profile"])), channel="chrome", headless=headless,
            viewport={"width": 1440, "height": 900},
        )
        try:
            yield ctx.pages[0] if ctx.pages else ctx.new_page()
        finally:
            ctx.close()


def login() -> None:
    with browser() as page:
        page.goto("https://whop.com/login/")
        input("Log in to Whop in the opened browser, then press Enter here... ")


def _app_frame(page, timeout: float = 30):
    """The Content Rewards UI renders inside a cross-origin iframe."""
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
    raise TimeoutError("Content Rewards iframe did not load (are you logged in? run: python -m clipper login)")


def _scroll_all(frame, rounds: int = 12) -> None:
    for _ in range(rounds):
        frame.evaluate("window.scrollBy(0, 2500)")
        frame.page.wait_for_timeout(700)


# ---------- scout ----------

class Listing(BaseModel):
    title: str = Field(description="campaign title exactly as shown (the line under the brand/age line)")
    brand: str | None
    content_type: str = Field(description="clipping | ugc | music | other (guess from title if not explicit)")
    headline_cpm: float | None = Field(description="the '$X/1K' number on the card")
    paid_out: float | None = Field(description="first number of '$A / $B' (already paid out); 5.3k -> 5300")
    budget_total: float | None = Field(description="second number of '$A / $B'")
    submissions: int | None = Field(description="the count shown on the card (clips/clippers)")
    requires_application: bool = Field(description="True if the card says 'Application'")


class ListingPage(BaseModel):
    campaigns: list[Listing]


class Detail(BaseModel):
    accepting_clips: bool
    youtube_cpm: float | None = Field(description="$ per 1K views for YouTube; null if YouTube not listed")
    youtube_min_payout: float | None
    youtube_max_payout: float | None
    best_other_cpm: float | None = Field(description="highest per-1K rate on any other platform")
    budget_paid_out: float | None = Field(description="the big $ number under 'Budget' (already paid out)")
    budget_remaining: float | None = Field(description="the '$X remaining' number")
    avg_clipper_earnings: float | None = Field(description="'Clippers in this campaign earn $X on average'")
    total_views: str | None
    requires_application: bool
    has_written_requirements: bool = Field(description="True if content/creator requirements are listed")
    category: str | None = Field(description="category tag shown (e.g. Gaming, Entertainment, Technology, Poker)")
    niche: str = Field(description="one of: gaming, esports, movies_tv, entertainment, music, tech, ai, finance, crypto, gambling, lifestyle, beauty, toys, manifestation, selfimprovement, faith, christian, inspirational, other")


def _prerank(L: Listing, s: dict) -> float:
    """Cheap ranking from the card alone, to decide which campaigns deserve a detail look."""
    if L.content_type.lower() not in s["content_types"] + ["music"] or not L.budget_total:
        return -1
    remaining = L.budget_total - (L.paid_out or 0)
    low_pct = 100 * remaining / L.budget_total < s["min_budget_remaining_pct"] and remaining < 5000
    if remaining < s["min_budget_remaining"] or low_pct:
        return -1
    fit = 8 if re.search(s.get("niche_regex", "$^"), f"{L.title} {L.brand}", re.I) else 0
    return (L.headline_cpm or 0) * 10 + min(remaining, 20000) / 2000 + fit


def _search(page, query: str):
    """Run the marketplace's own search (it matches beyond the ~20 featured campaigns)."""
    page.goto(cfg()["scout"]["marketplace_url"], wait_until="domcontentloaded", timeout=90000)
    frame = _app_frame(page)
    page.wait_for_timeout(3000)
    box = frame.locator("input[placeholder*='campaigns' i]").first
    box.click(force=True)
    box.fill(query)
    box.press("Enter")
    page.wait_for_timeout(9000)
    _scroll_all(frame, 4)
    return frame


def missing_campaigns(titles: list[str]) -> list[str]:
    """Titles whose campaign no longer shows up in Whop's search (paused, ended or removed by the brand).

    The search page echoes the query as its heading, so a campaign counts as present only when its
    stable title prefix appears again as a result card."""
    missing = []
    with browser(headless=True) as page:
        for title in titles:
            p = _prefix(title)
            try:
                frame = _search(page, p)
                hits = [l for l in frame.inner_text("body").splitlines() if l.strip().lower().startswith(p)]
                if len(hits) < 2:
                    missing.append(title)
            except Exception as err:  # a flaky page load isn't evidence the campaign is gone
                print(f"[check] couldn't check {title!r}: {err.__class__.__name__}")
    return missing


def _prefix(title: str) -> str:
    return re.split(r"\s[–—|-]\s|\s\|", title, maxsplit=1)[0].strip().lower()


def _known_id(title: str) -> str:
    """Brands rename campaigns ("Clip Ali Choucair – Real Estate Content" -> "... – 21 y/o, 51 Rentals").
    A new title whose stable prefix matches a campaign we already joined is that campaign, not a new one
    (a second copy would re-clip the same footage)."""
    cid = slug(title)
    if db.get("campaigns", cid):
        return cid
    p = _prefix(title)
    if len(p) >= 8:
        for c in db.rows("campaigns", "status IN ('joined','ended')"):
            if _prefix(c.get("title") or "") == p:
                print(f"[scout] {title!r} is the renamed {c['title']!r}")
                return c["id"]
    return cid


def scout(max_details: int = 12, query: str | None = None, new_only: bool = False) -> list[dict]:
    """Featured marketplace by default; with `query`, the marketplace search results instead."""
    s = cfg()["scout"]
    raw_dir = DATA / "scout_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with browser() as page:
        if query:
            frame = _search(page, query)
        else:
            page.goto(s["marketplace_url"], wait_until="domcontentloaded", timeout=90000)
            frame = _app_frame(page)
            page.wait_for_timeout(2500)
            _scroll_all(frame)
        text = frame.inner_text("body")
        (raw_dir / f"{stamp}-list.txt").write_text(text, encoding="utf-8")
        print(f"[scout] marketplace read ({len(text)} chars); extracting campaigns...")
        listings = llm.ask(
            "Extract every distinct campaign card from this Whop Content Rewards page text. "
            f"Skip duplicates and the 'Top clips' view counters.\n\n{text}", ListingPage).campaigns
        print(f"[scout] {len(listings)} campaigns on the page")

        ranked = []
        for L in listings:
            cid = _known_id(L.title)
            prev = db.get("campaigns", cid)
            if prev and prev["status"] in ("waitlisted", "applied"):
                continue
            if new_only and prev:  # the watcher only opens campaigns it has never seen
                continue
            pr = _prerank(L, s)
            if prev and prev["status"] == "joined":
                pr = 1000  # re-read joined campaigns first, to notice when they end or run out of budget
            if pr < 0:
                db.upsert("campaigns", {"id": cid, "title": L.title, "data": L.model_dump(), "status": "skipped", "score": 0})
            else:
                ranked.append((pr, cid, L))
        ranked.sort(key=lambda r: -r[0])

        results = []
        for i, (_, cid, L) in enumerate(ranked[:max_details]):
            if i:  # campaigns open as full pages in the member layout: start each from a fresh marketplace
                frame = None
                for attempt in range(3):  # the app iframe sometimes just doesn't come back
                    try:
                        if query:
                            frame = _search(page, query)
                            break
                        page.goto(s["marketplace_url"], wait_until="domcontentloaded", timeout=90000)
                        frame = _app_frame(page, timeout=45)
                        break
                    except Exception as err:
                        print(f"[scout] marketplace reload failed ({err.__class__.__name__}), retry {attempt + 1}/3")
                        page.wait_for_timeout(5000)
                if frame is None:
                    print("[scout] giving up on the remaining campaigns; results so far are saved")
                    break
                page.wait_for_timeout(2500)
                _scroll_all(frame, 6)
            detail_text = open_campaign(page, frame, L.title)
            if not detail_text:
                continue
            (raw_dir / f"{stamp}-{cid}.txt").write_text(detail_text, encoding="utf-8")
            try:
                d = llm.ask(f"Extract fields from this Whop campaign panel:\n\n{detail_text}", Detail)
            except RuntimeError as err:
                print(f"[scout] extraction failed for {L.title!r}: {str(err)[:160]}")
                if "limit" in str(err).lower():
                    print("[scout] Claude usage limit reached; stopping early (saved results so far).")
                    break
                continue
            score, status = _score(d, s)
            if (db.get("campaigns", cid) or {}).get("status") == "joined":
                status = "joined" if status == "shortlisted" else "ended"  # ended: stop making clips for it
            db.upsert("campaigns", {"id": cid, "title": L.title,
                                    "data": {**L.model_dump(), **d.model_dump(), "brief_text": detail_text},
                                    "score": score, "status": status})
            results.append({"id": cid, "title": L.title, "score": score, "status": status, **d.model_dump()})
            print(f"[scout] {status:11} {score:5.1f}  {L.title}")
    return sorted(results, key=lambda r: -r["score"])


def open_campaign(page, frame, title: str) -> str | None:
    """Opens the campaign's detail dialog; returns its text plus reference links."""
    def is_open() -> bool:
        # the campaign's own pay table must be visible (not just a card on the discover page)
        dlg = frame.locator("[role=dialog]")
        text = dlg.last.inner_text() if dlg.count() else frame.inner_text("body")
        return "Per 1K views" in text and "remaining" in text and title.lower()[:25] in text.lower()

    try:
        matches = frame.get_by_text(title, exact=True)
        opened = False
        # a title can appear in several sections (carousel, grid); try each, real click first, DOM click as fallback
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
                    if is_open():
                        opened = True
                        break
                if opened:
                    break
            if opened:
                break
        if not opened:
            raise TimeoutError("no match opened the campaign")
        page.wait_for_timeout(1500)
        dlg = frame.locator("[role=dialog]")
        root = dlg.last if dlg.count() else frame.locator("body")
        for _ in range(6):  # requirements/reference sections load as you scroll
            root.evaluate("e => (e.scrollHeight > e.clientHeight ? e : window).scrollBy(0, 1500)")
            page.wait_for_timeout(600)
        links = root.evaluate("d => [...d.querySelectorAll('a[href]')].map(a => (a.innerText.trim() || 'link') + ': ' + a.href)")
        return root.inner_text() + "\n\nLinks:\n" + "\n".join(links)
    except Exception as err:
        print(f"[scout] could not open {title!r}: {err.__class__.__name__}")
        return None


def close_campaign(page, frame) -> None:
    page.keyboard.press("Escape")
    page.wait_for_timeout(800)


def _score(d: Detail, s: dict) -> tuple[float, str]:
    cpm = d.youtube_cpm or d.best_other_cpm  # Instagram/TikTok-only campaigns are fine now
    if not d.accepting_clips or not cpm:
        return 0, "skipped"
    rem = d.budget_remaining or 0
    total = rem + (d.budget_paid_out or 0)
    pct = 100 * rem / total if total else 0
    # views a clip needs before it pays anything
    views_to_min = 1000 * (d.youtube_min_payout or 0) / cpm
    low_pct = pct < s["min_budget_remaining_pct"] and rem < 5000
    if (cpm < s["min_cpm"] or rem < s["min_budget_remaining"] or low_pct
            or views_to_min > s.get("max_views_to_min_payout", 20000)):
        return 0, "skipped"
    # YouTube pay rate matters most, then runway, then proof that clippers actually earn; penalize high minimums
    score = cpm * 10 + min(rem, 20000) / 2000 + min(pct, 100) / 20 - views_to_min / 2000
    if d.avg_clipper_earnings is not None:
        score += min(d.avg_clipper_earnings, 50) / 10
    if d.niche in s.get("avoid_niches", []):
        return 0, "skipped"
    if d.niche in s.get("preferred_niches", []):
        score += 10  # fits the channel's audience and the "niche page" rules many campaigns have
    return round(score, 2), "shortlisted"


# ---------- join / apply ----------

class Answers(BaseModel):
    answers: list[str] = Field(description="One answer per question, in order")


def join(campaign_id: str) -> None:
    c = db.get("campaigns", campaign_id)
    with browser() as page:
        page.goto(cfg()["scout"]["marketplace_url"], wait_until="domcontentloaded", timeout=90000)
        frame = _app_frame(page)
        _scroll_all(frame, 6)
        if not open_campaign(page, frame, c["title"]):
            return
        frame.get_by_role("button", name="Join Campaign").or_(frame.get_by_text("Join waitlist")).or_(
            frame.get_by_text("Apply")).first.click()
        page.wait_for_timeout(3000)
        text = frame.inner_text("body")
        fields = frame.locator("textarea, input[type=text]")
        if fields.count():
            qs = llm.ask(
                "Draft short, honest application answers for a YouTube Shorts clipper (channel @PogingPanda). "
                f"Application page text:\n\n{text}", Answers).answers
            for i, a in enumerate(qs[:fields.count()]):
                fields.nth(i).fill(a)
                print(f"Q{i+1} answer: {a}")
            print("\nReview the filled application in the browser and click its Submit button yourself.")
        else:
            print("Join clicked. Check the browser: if a confirmation/terms screen shows, review it yourself.")
        input("Press Enter when done... ")
    db.upsert("campaigns", {"id": campaign_id, "status": "joined"})


# ---------- submit ----------

# ---------- applications ----------

class ApplicationDraft(BaseModel):
    can_answer_truthfully: bool = Field(
        description="False if any question needs facts that are not in the material given (personal details, "
                    "earnings, analytics, a phone number, email, a screenshot/file, a promise about the account "
                    "owner's own actions): those must come from the human.")
    needs_human: list[str] = Field(description="The questions that can't be answered truthfully, and why")
    motivation: str = Field(description="2-3 sentences for 'I'm a good fit because...': specific to this campaign "
                                        "and these accounts, confident, no hype, no invented numbers")
    answers: list[str] = Field(description="One answer per screening question, in order; short and factual")


PLATFORM_NAMES = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok", "x": "X"}


def _open_by_search(page, title: str):
    """Opens a campaign page via Whop's search (works for campaigns not on the home page)."""
    frame = _search(page, _prefix(title))
    return frame if open_campaign(page, frame, title) else None


def application_status(title: str) -> str:
    """approved (can submit clips) | pending | rejected | apply (not applied yet) | unknown."""
    with browser(headless=True) as page:
        frame = _open_by_search(page, title)
        if frame is None:
            return "unknown"
        frame = _app_frame(page)
        text = frame.inner_text("body").lower()
        if frame.locator("button:has-text('Submit clip')").count():
            return "approved"
        if any(w in text for w in ("rejected", "declined", "not approved")):
            return "rejected"
        if any(w in text for w in ("pending", "under review", "application submitted", "applied")):
            return "pending"
        if frame.get_by_role("button", name="Apply").count():
            return "apply"
        return "unknown"


def apply(campaign: dict, accounts: list[dict], facts: str, dry_run: bool = False) -> tuple[str, str]:
    """Fills and submits the campaign's application. Returns (status, detail):
    applied | approved (already in) | needs_human (not submitted: why) | error."""
    title = campaign["title"]
    with browser(headless=True) as page:
        frame = _open_by_search(page, title)
        if frame is None:
            return "error", "campaign not found on Whop"
        frame = _app_frame(page)
        if frame.locator("button:has-text('Submit clip')").count():
            return "approved", "already accepted"
        btn = frame.get_by_role("button", name="Apply")
        if not btn.count():
            return "error", "no Apply button (maybe already applied, or a waitlist)"
        btn.first.click()
        page.wait_for_timeout(4000)
        dlg = frame.locator("[role=dialog]").last

        def cancel():
            try:
                dlg.get_by_role("button", name="Cancel").first.click(timeout=5000)
            except Exception:
                page.keyboard.press("Escape")

        # 1. apply with the accounts that fit this campaign
        picked = []
        for t in accounts:
            handle, plat = t["handle"].lstrip("@").lower(), PLATFORM_NAMES.get(t["platform"], t["platform"])
            for b in dlg.locator("button[aria-pressed]").all():
                txt = b.inner_text().lower()
                if f"@{handle}" in txt and plat.lower() in txt:
                    if b.get_attribute("aria-pressed") != "true":
                        b.click()
                    picked.append(f"@{handle} ({plat})")
                    break
        if not picked:
            cancel()
            return "needs_human", "none of the fitting accounts is connected in Whop's Content Rewards app"

        # 2. anything we can't fill honestly (uploads, dropdowns) stops the application
        if dlg.locator("input[type=file], select, [role=combobox]").count():
            cancel()
            return "needs_human", "the form asks for a file or a choice from a list"
        motivation_box = dlg.locator("textarea").first
        qs = dlg.locator("input[name^=q], textarea[name^=q]")
        questions = []
        for i in range(qs.count()):
            label = qs.nth(i).evaluate("e => e.closest('div')?.parentElement?.innerText || ''").strip()
            questions.append(label.split("\n")[0][:300] or f"question {i + 1}")

        refs = campaign_refs(campaign["id"])
        draft = llm.ask(
            f"Write an honest application for this Whop clipping campaign.\n\n<campaign>{title}</campaign>\n"
            f"<brief>\n{(campaign.get('data') or {}).get('brief_text', '')[:6000]}\n</brief>\n"
            f"<reference_materials>\n{refs[:12000]}\n</reference_materials>\n"
            f"<applying_with>{', '.join(picked)}</applying_with>\n<facts_about_us>\n{facts}\n</facts_about_us>\n"
            f"<screening_questions>\n" + "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions)) +
            "\n</screening_questions>\n\nUse only facts given here. Questions about the creator/brand are "
            "answered from the brief and reference materials. Never invent statistics, earnings, personal "
            "details or promises.", ApplicationDraft)
        if not draft.can_answer_truthfully or len(draft.answers) < len(questions):
            cancel()
            return "needs_human", "; ".join(draft.needs_human) or "some questions need you"

        # 3. fill in and submit
        if motivation_box.count():
            motivation_box.fill(draft.motivation)
        for i in range(qs.count()):
            qs.nth(i).fill(draft.answers[i])
        page.wait_for_timeout(1000)
        shots = DATA / "applications"
        shots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shots / f"{campaign['id']}-before.png"), full_page=True)
        if dry_run:  # rehearsal: everything filled in, then cancelled
            cancel()
            return "dry_run", (f"Accounts: {', '.join(picked)}\nMotivation: {draft.motivation}\n" +
                               "\n".join(f"Q: {q}\nA: {a}" for q, a in zip(questions, draft.answers)))
        dlg.get_by_role("button", name="Submit application").click(timeout=10000)
        for _ in range(20):
            page.wait_for_timeout(1500)
            text = frame.inner_text("body").lower()
            if not frame.locator("[role=dialog]").count() or any(
                    w in text for w in ("application submitted", "pending", "under review", "applied")):
                break
        page.screenshot(path=str(shots / f"{campaign['id']}-after.png"), full_page=True)
        still_open = frame.locator("[role=dialog]").count() and "submit application" in \
            frame.locator("[role=dialog]").last.inner_text().lower()
        if still_open:
            cancel()
            return "error", "Whop didn't accept the application form (a required field may be missing)"
        summary = (f"Accounts: {', '.join(picked)}\nMotivation: {draft.motivation}\n" +
                   "\n".join(f"Q: {q}\nA: {a}" for q, a in zip(questions, draft.answers)))
        return "applied", summary


def campaign_refs(campaign_id: str) -> str:
    p = DATA / "campaigns" / campaign_id / "references.md"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def submit(clip: dict, campaign: dict) -> bool:
    """Opens the campaign, fills the Submit form with the post URL + raw file, and waits for your confirmation."""
    with browser() as page:
        page.goto(campaign.get("url") or cfg()["scout"]["marketplace_url"])
        frame = _app_frame(page)
        _scroll_all(frame, 6)
        open_campaign(page, frame, campaign["title"])
        frame.get_by_role("button", name="Submit").first.click()
        page.wait_for_timeout(1500)
        url_box = frame.locator("input[type=url], input[placeholder*='http'], input[placeholder*='link' i]").first
        url_box.fill(clip["youtube_url"])
        files = frame.locator("input[type=file]")
        if files.count():
            files.first.set_input_files(str(Path(clip["file"]).resolve()))
        print(f"\nFilled submission for {clip['id']}: {clip['youtube_url']}")
        ok = input("Check the form in the browser. Type y to click the final Submit, anything else to skip: ").strip().lower() == "y"
        if ok:
            frame.get_by_role("button", name="Submit").last.click()
            page.wait_for_timeout(4000)
        return ok
