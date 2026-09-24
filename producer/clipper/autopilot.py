"""Unattended daily producer (runs on GitHub Actions with the PC off).

scout -> read briefs + reference docs -> join -> download footage -> clip -> compliance audit
-> auto-approve only what the audit clears for unattended posting -> hand to the publisher queue.

Every step is best-effort per campaign: one broken campaign never stops the others.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from . import db, targets
from .config import campaign_dir, cfg

# links in a brief that are footage we can fetch without a login or a terms click
FETCHABLE = re.compile(r"drive\.google\.com/(file|drive/folders|open)|dropbox\.com|youtube\.com|youtu\.be", re.I)
CHANNEL = re.compile(r"youtube\.com/(@[\w.-]+|channel/[\w-]+|c/[\w-]+)/?(videos|shorts|streams)?/?$", re.I)


def _language_ok(campaign: dict) -> bool:
    lang = ((campaign.get("checklist") or {}).get("content_language") or "english").lower()
    return lang in (cfg()["produce"].get("languages") or ["english", "none"])


def _linked_targets(campaign: dict) -> list[dict]:
    return [t for t in targets.for_campaign(campaign) if t.get("whop_linked") and t.get("auto_post")]


def fetch_sources(campaign: dict) -> list[Path]:
    """Downloads the campaign's footage; returns every video file available for it."""
    P = cfg()["produce"]
    ck = campaign.get("checklist") or {}
    dest = campaign_dir(campaign["id"]) / "sources"
    dest.mkdir(parents=True, exist_ok=True)
    for url in ck.get("source_assets", []):
        if not FETCHABLE.search(url):
            print(f"   skip source (needs a human or isn't footage): {url[:90]}")
            continue
        try:
            if "dropbox.com" in url:
                from . import dropbox

                dropbox.download(url, dest, max_mb=P["max_source_mb"])
            elif "drive.google.com" in url:
                from . import source

                source.download(url, dest)
            else:
                _ytdlp(url, dest, campaign["id"], P)
        except Exception as err:  # blocked, private, deleted...: try the next link
            print(f"   source failed ({err.__class__.__name__}): {url[:90]}")
    return sorted(p for p in dest.rglob("*") if p.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"})


def _ytdlp(url: str, dest: Path, campaign_id: str, P: dict) -> None:
    args = [sys.executable, "-m", "yt_dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
            "--merge-output-format", "mp4", "--no-progress",
            "--max-filesize", f"{P['max_source_mb']}M",
            "--match-filter", f"duration < {P['max_source_minutes'] * 60} & !is_live",
            "--download-archive", str(campaign_dir(campaign_id) / "yt_archive.txt"),
            "-o", str(dest / "%(title).80s [%(id)s].%(ext)s")]
    cookies = os.environ.get("YT_COOKIES_FILE")
    if cookies and Path(cookies).exists():
        args += ["--cookies", cookies]
    if CHANNEL.search(url):  # a whole channel: the next N videos not fetched before, newest first
        url = re.sub(r"/(videos|shorts|streams)?/?$", "", url) + "/videos"
        args += ["--playlist-end", "30", "--max-downloads", str(P["channel_videos"])]
    rc = subprocess.run(args + [url], timeout=1800).returncode
    if rc not in (0, 101):  # 101 = stopped at --max-downloads, which is the plan
        raise subprocess.CalledProcessError(rc, "yt-dlp")


def _unposted(campaign_id: str) -> int:
    """Clips of this campaign still waiting in the publisher queue for at least one account."""
    from .export import PUBLISHER

    state = PUBLISHER / "state" / "posts.json"
    posts = json.loads(state.read_text(encoding="utf-8"))["posts"] if state.exists() else []
    done = {(p["clip_id"], p["target_id"]) for p in posts if p["status"] in ("posted", "draft_uploaded")}
    n = 0
    for meta in (PUBLISHER / "queue").glob("*/meta.json"):
        m = json.loads(meta.read_text(encoding="utf-8"))
        if m["campaign_id"] == campaign_id and any((m["clip_id"], t) not in done for t in m["targets"]):
            n += 1
    return n


def _producible(campaign: dict) -> tuple[bool, list[str]]:
    """Can the unattended pipeline make, post and submit clips for it? Returns (ok, reasons it can't).

    Payout-only paperwork (demographic proof once a clip earns, a payout form) doesn't stop production
    when produce.allow_payout_paperwork is on: the clips post and submit normally, and you do the
    paperwork for the ones that reach a payout."""
    ck = campaign.get("checklist") or {}
    if campaign["id"] in (cfg()["produce"].get("override_campaigns") or []):
        return True, []
    if "production_blockers" not in ck:  # analyzed before the split: fall back to the old verdict
        return bool(ck.get("auto_ok")), list(ck.get("auto_blockers") or ["brief not analyzed yet"])
    why = list(ck.get("production_blockers") or [])
    if ck.get("payout_steps") and not cfg()["produce"].get("allow_payout_paperwork"):
        why += [f"payout paperwork: {s}" for s in ck["payout_steps"]]
    return not why, why


def _queued_for(target_id: str) -> int:
    """Clips waiting in the publisher queue that this account hasn't posted yet."""
    from .export import PUBLISHER

    state = PUBLISHER / "state" / "posts.json"
    posts = json.loads(state.read_text(encoding="utf-8"))["posts"] if state.exists() else []
    done = {p["clip_id"] for p in posts if p["target_id"] == target_id and p["status"] in ("posted", "draft_uploaded")}
    n = 0
    for meta in (PUBLISHER / "queue").glob("*/meta.json"):
        m = json.loads(meta.read_text(encoding="utf-8"))
        if target_id in m.get("targets", []) and m["clip_id"] not in done:
            n += 1
    return n


def _deficits() -> dict[str, float]:
    """How many more approved clips each account needs to fill its posting slots for the next
    produce.buffer_days (the queue should never run dry between daily runs)."""
    P = cfg()["produce"]
    out = {}
    for t in targets.all_targets():
        if not (t.get("auto_post") and t.get("whop_linked")):
            continue
        per_day = cfg().get("cadence", {}).get(t["platform"], {}).get("per_day", 3)
        out[t["id"]] = per_day * P.get("buffer_days", 1.5) - _queued_for(t["id"])
    return out


def _pass_rate() -> float:
    """Share of rendered clips the audit clears, so enough are made to fill the slots."""
    done = [c for c in db.rows("clips", "status IN ('approved','queued','held','posted','submitted')")]
    ok = [c for c in done if c["status"] != "held"]
    return max(0.3, len(ok) / len(done)) if len(done) >= 6 else 0.4


def _search_plan(discover: bool) -> list[str]:
    """Niche searches for this run: every niche whose accounts have nothing to post comes first."""
    import datetime

    by_niche = cfg()["scout"].get("search_queries_by_niche", {})
    live = [c for c in db.rows("campaigns", "status='joined'") if _producible(c)[0] and _language_ok(c)]
    fed = {t["id"] for c in live for t in _linked_targets(c)}
    hungry_niches = []
    for t in targets.all_targets():
        if t.get("auto_post") and t.get("whop_linked") and t["id"] not in fed:
            hungry_niches += [n for n in t.get("niches", []) if n in by_niche and n not in hungry_niches]
    all_q = [q for qs in by_niche.values() for q in qs]
    if discover:
        return list(dict.fromkeys(all_q))
    k = cfg()["produce"].get("searches_per_run", 2)
    day = datetime.date.today().toordinal()
    plan = []
    for n in hungry_niches:  # one per hungry niche, rotating daily
        qs = by_niche[n]
        plan.append(qs[day % len(qs)])
    start = (day * k) % max(len(all_q), 1)
    plan += (all_q[start:] + all_q[:start])[:k]
    return list(dict.fromkeys(plan))[:cfg()["produce"].get("max_searches_per_run", 6)]


def _approve(campaign_id: str | None, report: dict) -> int:
    """Auto-approve what the audit cleared for unattended posting; hold the rest. Returns approvals."""
    from . import compliance

    n = 0
    where, args = ("status='rendered' AND campaign_id=?", (campaign_id,)) if campaign_id else ("status='rendered'", ())
    for clip in db.rows("clips", where, *args):
        audit = (clip.get("qa") or {}).get("audit")
        if not audit:
            try:
                compliance.audit_campaign(clip["campaign_id"], only_new=True)
            except Exception as err:
                report["errors"].append(f"audit {clip['id']}: {str(err)[:150]}")
            clip = db.get("clips", clip["id"])
            audit = (clip.get("qa") or {}).get("audit") or {}
        if clip["status"] == "rendered" and audit.get("overall_pass") and audit.get("safe_to_autopost"):
            db.upsert("clips", {"id": clip["id"], "status": "approved"})
            report["approved"].append(clip["id"])
            n += 1
        else:
            db.upsert("clips", {"id": clip["id"], "status": "held"})  # waits for you: python -m clipper review
            report["held"].append({"id": clip["id"], "why": (audit.get("blocking_issues") or
                                                             audit.get("human_todo") or ["audit not cleared"])[:3]})
    return n


class _Submission(BaseModel):
    campaign: str = Field(description="campaign title as shown")
    date: str = Field(description="submission date as shown")
    views: int = Field(description="view count shown for the clip (0 if none)")
    status: str = Field(description="pending | approved | rejected | other")


class _SubmissionList(BaseModel):
    submissions: list[_Submission]


def paperwork_check(report: dict) -> None:
    """Campaigns with payout paperwork: when a submitted clip reaches its minimum payout, message you on
    Telegram (a GitHub issue if Telegram isn't set up) saying exactly what to send, so the earnings aren't lost."""
    import os

    from . import llm, whop

    campaigns = [c for c in db.rows("campaigns", "status IN ('joined','ended')")
                 if (c.get("checklist") or {}).get("payout_steps")]
    if not campaigns:
        return
    with whop.browser(headless=True) as page:
        page.goto(cfg()["scout"]["marketplace_url"], wait_until="domcontentloaded", timeout=90000)
        frame = whop._app_frame(page, 60)
        page.wait_for_timeout(3000)
        frame.get_by_text("Submissions", exact=True).first.click(force=True)
        page.wait_for_timeout(8000)
        whop._scroll_all(frame, 6)
        text = frame.inner_text("body")
    subs = llm.ask(f"List every submission on this Whop 'Your submissions' page.\n\n{text}", _SubmissionList).submissions
    for c in campaigns:
        d, ck = c.get("data") or {}, c["checklist"]
        cpm = d.get("youtube_cpm") or d.get("best_other_cpm") or 0
        min_views = 1000 * (d.get("youtube_min_payout") or 0) / cpm if cpm else 0
        prefix = whop._prefix(c["title"])
        for s in subs:
            if whop._prefix(s.campaign) != prefix or s.status == "rejected" or s.views < max(min_views, 1):
                continue
            title = f"Payout paperwork due: {s.campaign} ({s.date}, {s.views:,} views)"
            body = (f"A clip for **{s.campaign}** has {s.views:,} views, past the minimum payout "
                    f"(~{min_views:,.0f} views). To get it paid:\n\n" + "\n".join(f"- {x}" for x in ck["payout_steps"])
                    + "\n\nOpen Whop → Content Rewards → Submissions → View details on that clip for its submission ID.")
            report.setdefault("paperwork", []).append(title)
            if os.environ.get("GITHUB_ACTIONS"):  # one Telegram message per clip, the first day it qualifies
                from .notify import once

                once(f"paperwork|{s.campaign}|{s.date}|{s.views // 1000}", "💰 " + title, body)


def produce(discover: bool = False) -> dict:
    """discover=True: every niche search, more campaigns opened, every brief read (a one-off sweep)."""
    import math
    import time as _time

    from . import brief, whop
    from .__main__ import cmd_clip

    P = cfg()["produce"]
    t0 = _time.time()
    report = {"scouted": 0, "analyzed": [], "joined": [], "clips_made": 0, "approved": [],
              "held": [], "blocked_campaigns": {}, "errors": [], "unfilled": {}}

    print("== 1. scout (featured + niche searches)")
    for q in [None] + _search_plan(discover):
        try:
            report["scouted"] += len(whop.scout(query=q, max_details=(15 if discover else 12) if q is None
                                                else (8 if discover else 5)))
        except Exception as err:
            report["errors"].append(f"scout {q or 'featured'}: {err.__class__.__name__}: {str(err)[:150]}")
            if "limit" in str(err).lower():
                break

    print("\n== payout paperwork check")
    try:
        paperwork_check(report)
    except Exception as err:
        report["errors"].append(f"paperwork check: {err.__class__.__name__}: {str(err)[:150]}")

    print("\n== 2. read briefs + reference materials")
    todo = [c for c in db.rows("campaigns", "status IN ('shortlisted','joined') ORDER BY score DESC")
            if "production_blockers" not in (c.get("checklist") or {})]
    for c in todo[:999 if discover else P["analyze_per_run"]]:
        text = (c.get("data") or {}).get("brief_text")
        if not text:
            continue
        try:
            brief.analyze(c["id"], text)
            report["analyzed"].append(c["id"])
        except Exception as err:
            report["errors"].append(f"analyze {c['id']}: {str(err)[:150]}")

    print("\n== 3. join campaigns that fit an account and need no human steps")
    for c in db.rows("campaigns", "status='shortlisted'"):
        ck = c.get("checklist") or {}
        if not ck or not P.get("auto_join"):
            continue
        ok, why = _producible(c)
        if (c.get("data") or {}).get("requires_application"):
            why.append("requires an application")
        if not _linked_targets(c):
            why.append("no linked account in its niche/platforms")
        if not _language_ok(c):
            why.append(f"footage/audience is {ck.get('content_language')}; the accounts' audiences are English")
        if why:
            report["blocked_campaigns"][c["id"]] = why
            continue
        # the current Content Rewards layout has no join step: the first submission joins
        db.upsert("campaigns", {"id": c["id"], "status": "joined"})
        report["joined"].append(c["id"])
        print(f"   joined {c['title']}")

    print("\n== 4. make clips until every account's slots are covered")
    deficit = _deficits()
    rate = _pass_rate()
    print("   open slots per account: " + ", ".join(f"{k}={v:g}" for k, v in deficit.items()))
    budget = P["max_new_clips_per_run"]
    for c in db.rows("campaigns", "status='joined' ORDER BY score DESC"):
        if budget <= 0 or (_time.time() - t0) / 60 > P.get("max_minutes", 150):
            break
        ck = c.get("checklist") or {}
        ok, why = _producible(c)
        if not ok:
            report["blocked_campaigns"][c["id"]] = why
            continue
        if not _language_ok(c):
            report["blocked_campaigns"][c["id"]] = [f"{ck.get('content_language')} footage/audience"]
            continue
        tg = [t["id"] for t in _linked_targets(c)]
        need = max([deficit.get(t, 0) for t in tg] or [0])
        if need <= 0:
            continue
        # render more than needed: the audit holds some back
        room = min(math.ceil(need / rate), P.get("max_clips_per_campaign", 10), budget)
        print(f"\n-- {c['title'][:60]} (needs {need:g} approved -> rendering up to {room})")
        try:
            vids = fetch_sources(c)
            if not vids:
                report["errors"].append(f"{c['id']}: no footage could be downloaded")
                continue
            made = cmd_clip(c["id"], *map(str, vids), limit=room)
            report["clips_made"] += made
            budget -= made
            approved = _approve(c["id"], report)
            for t in tg:
                deficit[t] = deficit.get(t, 0) - approved
        except SystemExit as err:
            report["errors"].append(f"{c['id']}: {err}")
        except Exception as err:
            report["errors"].append(f"{c['id']}: {err.__class__.__name__}: {str(err)[:200]}")

    print("\n== 5. approve anything left over")
    _approve(None, report)
    names = {t["id"]: t["label"] for t in targets.all_targets()}
    report["unfilled"] = {names.get(k, k): round(v, 1) for k, v in deficit.items() if v > 0}

    print("\n== 6. queue for the publisher")
    from . import export

    export.publisher_config()
    export.export_queue()
    return report


def summary_telegram(r: dict) -> str:
    """The daily run in a few lines, for your phone."""
    lines = [f"🎬 Daily clips: {len(r['approved'])} queued, {len(r['held'])} held, {r['clips_made']} made",
             f"🔎 {r['scouted']} campaigns checked, {len(r['analyzed'])} briefs read"]
    if r["joined"]:
        lines.append("✅ Joined: " + ", ".join(r["joined"]))
    if r.get("unfilled"):
        lines.append("🕳 Empty slots: " + ", ".join(f"{k} {v:g}" for k, v in r["unfilled"].items()))
    if r.get("paperwork"):
        lines.append(f"💰 {len(r['paperwork'])} clip(s) need payout paperwork (separate messages)")
    if r["errors"]:
        lines.append(f"⚠️ {len(r['errors'])} error(s) - see the run summary on GitHub")
    return "\n".join(lines)


def summary_md(r: dict) -> str:
    lines = [f"**Scouted** {r['scouted']} · **analyzed** {len(r['analyzed'])} · **joined** {len(r['joined'])} · "
             f"**clips made** {r['clips_made']} · **auto-approved** {len(r['approved'])} · **held** {len(r['held'])}", ""]
    if r["joined"]:
        lines += ["### Joined", *[f"- {x}" for x in r["joined"]], ""]
    if r["approved"]:
        lines += ["### Queued for posting", *[f"- {x}" for x in r["approved"]], ""]
    if r["held"]:
        lines += ["### Held for your review (not posted)", *[f"- {h['id']}: {'; '.join(h['why'])[:200]}" for h in r["held"]], ""]
    if r["blocked_campaigns"]:
        lines += ["### Campaigns that need a human", *[f"- {k}: {'; '.join(v)[:200]}" for k, v in r["blocked_campaigns"].items()], ""]
    if r.get("paperwork"):
        lines += ["### Payout paperwork due (an issue is opened for each)", *[f"- {x}" for x in r["paperwork"]], ""]
    if r.get("unfilled"):
        lines += ["### Slots still empty (clips short per account)",
                  *[f"- {k}: {v:g}" for k, v in r["unfilled"].items()], ""]
    if r["errors"]:
        lines += ["### Errors", *[f"- {e}" for e in r["errors"]], ""]
    return "\n".join(lines)
