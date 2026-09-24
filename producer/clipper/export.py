"""Hand approved clips (and the Whop login session) to the cloud publisher repo."""
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import db, targets
from .config import ROOT, cfg, path

# locally the publisher repo sits in ./publisher; in the cloud the producer runs inside that repo
PUBLISHER = Path(os.environ.get("PUBLISHER_DIR", ROOT / "publisher"))
QUEUE_RELEASE = os.environ.get("QUEUE_RELEASE")  # cloud: videos go to this GitHub release, not into git


def platform_mentions(checklist: dict | None) -> dict:
    """{platform: {"@required": "@handle_on_that_platform"}} for creators whose handle differs by platform.

    The brief's links show the real accounts (youtube.com/@alichoucairr, instagram.com/alichoucair); a
    required tag that's a near-match of one of them is swapped for it on that platform only."""
    import re
    from difflib import SequenceMatcher

    ck = checklist or {}
    found = {}
    for url in ck.get("source_assets", []) or []:
        for platform, pat in (("youtube", r"youtube\.com/@([\w.-]+)"), ("tiktok", r"tiktok\.com/@([\w.]+)"),
                              ("instagram", r"instagram\.com/(?!p/|reel/|reels/|stories/)([\w.]+)")):
            m = re.search(pat, url, re.I)
            if m:
                found.setdefault(platform, m.group(1).rstrip("."))
    required = {t for req in ck.get("required_in_caption", []) or [] for t in re.findall(r"@[\w.]+", req)}
    out: dict = {}
    for tag in required:
        base = tag[1:].rstrip(".").lower()
        for platform, handle in found.items():
            h = handle.lower()
            if h != base and SequenceMatcher(None, h, base).ratio() >= 0.8:
                out.setdefault(platform, {})[tag.rstrip(".")] = "@" + handle
    return out


def export_queue() -> int:
    """Copies every approved, audited clip into publisher/queue/<clip_id>/."""
    queue = PUBLISHER / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    n = 0
    for c in db.rows("clips", "status='approved'"):
        campaign = db.get("campaigns", c["campaign_id"])
        tgts = [t["id"] for t in targets.for_campaign(campaign)]
        if not tgts:
            print(f"skip {c['id']}: no account matches {campaign['title']}")
            continue
        audit = (c.get("qa") or {}).get("audit") or {}
        if not audit.get("overall_pass"):
            print(f"skip {c['id']}: has not passed the compliance audit")
            continue
        d = queue / c["id"]
        d.mkdir(exist_ok=True)
        asset = None
        if QUEUE_RELEASE:
            asset = f"{c['id']}.mp4"
            tmp = Path(c["file"]).with_name(asset)
            if tmp != Path(c["file"]):
                shutil.copy2(c["file"], tmp)
            subprocess.run(["gh", "release", "upload", QUEUE_RELEASE, str(tmp), "--clobber"],
                           cwd=PUBLISHER, check=True)
        else:
            shutil.copy2(c["file"], d / "clip.mp4")
        m = c["meta"]
        (d / "meta.json").write_text(json.dumps({
            "clip_id": c["id"],
            "campaign_id": c["campaign_id"],
            "campaign_title": campaign["title"],
            "campaign_url": (campaign.get("data") or {}).get("campaign_url", ""),
            "title": m["title"],
            "caption": f"{m['description']}\n\n{' '.join(m['hashtags'])}",
            "hashtags": m["hashtags"],
            "score": m.get("score", 0),
            "targets": tgts,
            "submit_within_minutes": cfg()["whop"]["submit_deadline_minutes"],
            **({"video_asset": asset} if asset else {}),
            "mentions": platform_mentions(campaign.get("checklist")),
        }, indent=2), encoding="utf-8")
        db.upsert("clips", {"id": c["id"], "status": "queued"})
        n += 1
        print(f"queued {c['id']} -> {', '.join(tgts)}")
    print(f"\n{n} clip(s) in {queue}")
    return n


def export_session() -> Path:
    """Saves the Whop login as a Playwright storage_state file for the WHOP_SESSION secret."""
    from playwright.sync_api import sync_playwright

    out = ROOT / "secrets" / "whop_session.json"
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(path(cfg()["whop"]["browser_profile"])), channel="chrome", headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://whop.com/home/", wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(5000)
            if "/login" in page.url:
                raise RuntimeError("not logged in - run: python -m clipper login")
            ctx.storage_state(path=str(out))
        finally:
            ctx.close()
    print(f"saved {out} (upload as the WHOP_SESSION secret; refresh it when Whop logs out)")
    return out


def sync_secrets(repo: str = "cloudstrive6/whop-clipper-publisher") -> None:
    """Renew what expires (Instagram tokens, Whop session) and push every credential to GitHub.

    Values are piped from files straight into `gh secret set`; they are never printed.
    Run monthly, or whenever a run reports a stale Whop session.
    """
    import subprocess

    from . import instagram

    instagram.refresh_tokens(min_age_days=0)
    export_session()
    secrets = {"INSTAGRAM_TOKENS": ROOT / "secrets" / "instagram_tokens.json",
               "POSTFORME_API_KEY": ROOT / "secrets" / "postforme_api_key.txt",
               "WHOP_SESSION": ROOT / "secrets" / "whop_session.json"}
    for t in cfg()["targets"]:
        if t["platform"] == "youtube":
            secrets[f"YOUTUBE_TOKEN_{t['id'].upper()}"] = path(t["token"])
    for name, file in secrets.items():
        if not file.exists():
            print(f"  skip {name}: {file.name} missing")
            continue
        with open(file, "rb") as fh:
            subprocess.run(["gh", "secret", "set", name, "--repo", repo], stdin=fh, check=True,
                           capture_output=True)
        print(f"  updated {name}")
    print("all credentials synced to GitHub")


def publisher_config() -> Path:
    """The cloud repo gets targets + cadence + youtube settings, and no secrets."""
    c = cfg()
    out = {
        "targets": [{k: v for k, v in t.items() if k != "token"} | (
            {"token_env": f"YOUTUBE_TOKEN_{t['id'].upper()}"} if t["platform"] == "youtube" else {})
            for t in c["targets"]],
        "cadence": c.get("cadence", {}),
        "youtube": {k: v for k, v in c["youtube"].items() if k in
                    ("privacy_on_upload", "category_id", "geo_location", "post_window_et")},
        "whop": {"submit_deadline_minutes": c["whop"]["submit_deadline_minutes"]},
    }
    import yaml

    p = PUBLISHER / "config.yaml"
    p.write_text(yaml.safe_dump(out, sort_keys=False), encoding="utf-8")
    print(f"wrote {p}")
    return p


def sync_producer(with_state: bool = False, push: bool = True) -> None:
    """Copies the producer code, config and campaign references into the cloud repo (publisher/producer/).

    with_state also copies data/state.db; do that once when moving production to the cloud. After that the
    cloud owns the database: use `pull-state` to bring its copy back to the PC.
    """
    dst = PUBLISHER / "producer"
    (dst / "clipper").mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "clipper").glob("*.py"):
        shutil.copy2(f, dst / "clipper" / f.name)
    shutil.copy2(ROOT / "config.yaml", dst / "config.yaml")
    shutil.copy2(ROOT / "requirements-producer.txt", dst / "requirements.txt")
    for camp in (ROOT / "data" / "campaigns").glob("*"):
        for f in camp.iterdir():  # briefs' reference docs and PDFs, never footage or renders
            if f.is_file() and f.suffix.lower() in {".md", ".pdf", ".txt", ".json"}:
                (dst / "data" / "campaigns" / camp.name).mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst / "data" / "campaigns" / camp.name / f.name)
    if with_state:
        shutil.copy2(ROOT / "data" / "state.db", dst / "data" / "state.db")
    print(f"synced producer -> {dst}")
    if push:
        git = ["git", "-C", str(PUBLISHER)]
        subprocess.run(git + ["pull", "--rebase", "--autostash"], check=True)
        subprocess.run(git + ["add", "producer"], check=True)
        if subprocess.run(git + ["diff", "--staged", "--quiet"]).returncode:
            subprocess.run(git + ["commit", "-m", "Sync producer code/config"], check=True)
            subprocess.run(git + ["push"], check=True)
            print("pushed")


def pull_state() -> None:
    """Brings the cloud producer's database (campaigns, clips, audits) back to the PC."""
    subprocess.run(["git", "-C", str(PUBLISHER), "pull", "--rebase", "--autostash"], check=True)
    src = PUBLISHER / "producer" / "data" / "state.db"
    shutil.copy2(src, ROOT / "data" / "state.db")
    print("local data/state.db now matches the cloud")
