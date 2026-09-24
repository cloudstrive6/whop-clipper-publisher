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

from . import db, targets
from .config import campaign_dir, cfg

# links in a brief that are footage we can fetch without a login or a terms click
FETCHABLE = re.compile(r"drive\.google\.com/(file|drive/folders|open)|dropbox\.com|youtube\.com|youtu\.be", re.I)
CHANNEL = re.compile(r"youtube\.com/(@[\w.-]+|channel/[\w-]+|c/[\w-]+)/?(videos|shorts|streams)?/?$", re.I)


def _auto_ok(campaign: dict) -> bool:
    """The brief needs no human step, or you opted the campaign in (you handle its payout paperwork)."""
    overrides = cfg()["produce"].get("override_campaigns") or []
    return bool((campaign.get("checklist") or {}).get("auto_ok")) or campaign["id"] in overrides


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
    if CHANNEL.search(url):  # a whole channel: only its newest uploads
        url = re.sub(r"/(videos|shorts|streams)?/?$", "", url) + "/videos"
        args += ["--playlist-end", str(P["channel_videos"])]
    subprocess.run(args + [url], check=True, timeout=1800)


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


def produce() -> dict:
    from . import brief, compliance, whop
    from .__main__ import cmd_clip

    P = cfg()["produce"]
    report = {"scouted": 0, "analyzed": [], "joined": [], "clips_made": 0, "approved": [],
              "held": [], "blocked_campaigns": {}, "errors": []}

    print("== 1. scout (featured + niche searches)")
    import datetime

    queries = cfg()["scout"].get("search_queries", [])
    k = P.get("searches_per_run", 2)
    start = (datetime.date.today().toordinal() * k) % max(len(queries), 1)
    todays = (queries[start:] + queries[:start])[:k]
    for q in [None] + todays:
        try:
            report["scouted"] += len(whop.scout(query=q, max_details=12 if q is None else 5))
        except Exception as err:
            report["errors"].append(f"scout {q or 'featured'}: {err.__class__.__name__}: {str(err)[:150]}")
            if "limit" in str(err).lower():
                break

    print("\n== 2. read briefs + reference materials")
    todo = [c for c in db.rows("campaigns", "status IN ('shortlisted','joined') ORDER BY score DESC")
            if "content_language" not in (c.get("checklist") or {})][:P["analyze_per_run"]]
    for c in todo:
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
        why = ([] if _auto_ok(c) else ck.get("auto_blockers") or ["brief needs a human step"])
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

    print("\n== 4. make clips")
    budget = P["max_new_clips_per_run"]
    for c in db.rows("campaigns", "status='joined' ORDER BY score DESC"):
        if budget <= 0:
            break
        ck = c.get("checklist") or {}
        if not _auto_ok(c):
            report["blocked_campaigns"][c["id"]] = ck.get("auto_blockers") or ["not analyzed yet"]
            continue
        if not _linked_targets(c):
            continue
        if not _language_ok(c):
            report["blocked_campaigns"][c["id"]] = [f"{ck.get('content_language')} footage/audience"]
            continue
        room = P["queue_target_per_campaign"] - _unposted(c["id"])
        if room <= 0:
            print(f"   {c['title'][:50]}: queue already full")
            continue
        print(f"\n-- {c['title'][:60]} (room for {room})")
        try:
            vids = fetch_sources(c)
            if not vids:
                report["errors"].append(f"{c['id']}: no footage could be downloaded")
                continue
            made = cmd_clip(c["id"], *map(str, vids), limit=min(room, budget))
            report["clips_made"] += made
            budget -= made
        except SystemExit as err:
            report["errors"].append(f"{c['id']}: {err}")
        except Exception as err:
            report["errors"].append(f"{c['id']}: {err.__class__.__name__}: {str(err)[:200]}")

    print("\n== 5. auto-approve what the audit cleared for unattended posting")
    for clip in db.rows("clips", "status='rendered'"):
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
        else:
            db.upsert("clips", {"id": clip["id"], "status": "held"})  # waits for you: python -m clipper review
            report["held"].append({"id": clip["id"], "why": (audit.get("blocking_issues") or
                                                             audit.get("human_todo") or ["audit not cleared"])[:3]})

    print("\n== 6. queue for the publisher")
    from . import export

    export.publisher_config()
    export.export_queue()
    return report


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
    if r["errors"]:
        lines += ["### Errors", *[f"- {e}" for e in r["errors"]], ""]
    return "\n".join(lines)
