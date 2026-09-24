"""Cloud publisher: post one queued clip per account, then submit it on Whop.

Runs on GitHub Actions, fired by cron-job.org (one call per time slot). No AI, no rendering: every clip
was made and compliance-audited by the producer (producer/, daily) before it entered the queue.

  python publish.py --platform youtube          # every account on that platform, one clip each
  python publish.py --platform tiktok --account tt_cryptohustler
  python publish.py --platform all --dry-run
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
QUEUE = ROOT / "queue"
STATE = ROOT / "state" / "posts.json"      # older records
POSTS_DIR = ROOT / "state" / "posts.d"      # one file per post: overlapping runs never clash in git
RELEASE = "queue"  # cloud-made clips live as assets of this GitHub release, not in git


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict:
    posts = json.loads(STATE.read_text(encoding="utf-8"))["posts"] if STATE.exists() else []
    for f in sorted(POSTS_DIR.glob("*.json")):
        posts.append(json.loads(f.read_text(encoding="utf-8")) | {"_file": f.name})
    return {"posts": posts}


def save_state(state: dict) -> None:
    """New records (and their later 'submitted' updates) live in their own files."""
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    for p in state["posts"]:
        if "_file" not in p and p.get("status") != "dry_run" and "at" in p and p.get("_new"):
            p["_file"] = f"{p['at'].replace(':', '')}_{p['target_id']}.json"
        if p.get("_file"):
            rec = {k: v for k, v in p.items() if not k.startswith("_")}
            (POSTS_DIR / p["_file"]).write_text(json.dumps(rec, indent=2), encoding="utf-8")


def git(*args: str) -> bool:
    return subprocess.run(["git", "-c", "user.name=whop-clipper", "-c", "user.email=bot@users.noreply.github.com",
                           *args], cwd=ROOT, capture_output=True).returncode == 0


def share_state(msg: str) -> None:
    """Push post records immediately, so a run that overlaps this one sees them (no double posts)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    git("add", "state")
    if git("diff", "--staged", "--quiet"):
        return
    git("commit", "-m", msg)
    for _ in range(3):
        if git("pull", "--rebase", "--autostash") and git("push"):
            return
    print("::warning::could not share post records yet (they're saved again at the end of the run)")


def refresh_state() -> dict:
    """Latest records from other runs before deciding what to post."""
    if os.environ.get("GITHUB_ACTIONS"):
        git("pull", "--rebase", "--autostash")
    return load_state()


def config() -> dict:
    import yaml

    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def queue_items() -> list[dict]:
    """Each queued clip is queue/<clip_id>/meta.json next to clip.mp4."""
    out = []
    for meta in sorted(QUEUE.glob("*/meta.json")):
        d = json.loads(meta.read_text(encoding="utf-8"))
        d["dir"] = meta.parent
        d["file"] = meta.parent / "clip.mp4"
        out.append(d)
    return out


def posted_count(state: dict, target_id: str, hours: int = 24) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return sum(1 for p in state["posts"]
               if p["target_id"] == target_id and p["status"] in ("posted", "draft_uploaded")
               and datetime.fromisoformat(p["at"]) > cutoff)


def already_posted(state: dict, clip_id: str, target_id: str) -> bool:
    return any(p["clip_id"] == clip_id and p["target_id"] == target_id
               and p["status"] in ("posted", "draft_uploaded") for p in state["posts"])


def next_clip(state: dict, target: dict, clips: list[dict]) -> dict | None:
    """Highest-scoring approved clip for this account's campaigns that it hasn't posted yet."""
    eligible = [c for c in clips
                if target["id"] in c.get("targets", [])
                and not already_posted(state, c["clip_id"], target["id"])]
    return max(eligible, key=lambda c: c.get("score", 0), default=None)


def with_mentions(text: str, clip: dict, platform: str) -> str:
    """Swaps a creator's handle for their handle on this platform (e.g. @alichoucair on Instagram
    is @alichoucairr on YouTube), so the tag points at the right account everywhere."""
    import re

    for handle, local in (clip.get("mentions") or {}).get(platform, {}).items():
        # not part of a longer handle (@alichoucairr, @ali.choucair); a sentence's full stop is fine
        text = re.sub(re.escape(handle) + r"(?!\w|\.\w)", local, text, flags=re.I)
    return text


def unsubmitted_alert(clip: dict, target: dict, url: str, why: str) -> None:
    """Whop only accepts a link for ~30 minutes after posting: tell you straight away."""
    from notify import alert

    alert(f"⚠️ Submit this on Whop now (30-min window): {target['label']}",
          f"Campaign: {clip['campaign_title']}\nLink: {url}\n\nThe automatic submission failed ({why}).\n"
          "Whop → Content Rewards → the campaign → Submit clip → paste the link.")


def ensure_video(clip: dict) -> None:
    if clip["file"].exists() or not clip.get("video_asset"):
        return
    subprocess.run(["gh", "release", "download", RELEASE, "-p", clip["video_asset"], "-D", str(clip["dir"]),
                    "--clobber"], cwd=ROOT, check=True)
    (clip["dir"] / clip["video_asset"]).rename(clip["file"])


def retire_finished(state: dict, clips: list[dict]) -> None:
    """A clip posted to every one of its accounts leaves the queue (and its release asset is deleted)."""
    for c in clips:
        if all(already_posted(state, c["clip_id"], t) for t in c.get("targets", [])):
            if c.get("video_asset"):
                subprocess.run(["gh", "release", "delete-asset", RELEASE, c["video_asset"], "-y"], cwd=ROOT)
            shutil.rmtree(c["dir"], ignore_errors=True)
            print(f"retired {c['clip_id']} (posted everywhere)")


def post_clip(clip: dict, target: dict, cfg: dict, dry: bool) -> tuple[str, str, str]:
    """Returns (status, url, note)."""
    caption = with_mentions(clip["caption"], clip, target["platform"])
    if dry:
        return "dry_run", "", f"would post {clip['clip_id']} to {target['label']}"
    ensure_video(clip)
    if target["platform"] == "youtube":
        import platforms.youtube as yt

        url = yt.upload(clip["file"], clip["title"], caption, clip.get("hashtags", []), target, cfg)
        return "posted", url, ""
    if target["platform"] == "instagram":
        import platforms.instagram as ig

        url = ig.publish_reel(clip["file"], caption, target)
        return "posted", url, ""
    if target["platform"] in ("tiktok", "x"):
        import platforms.postforme as pfm

        url, note, drafted = pfm.post(clip["file"], caption, target, sponsored=True)
        return ("draft_uploaded" if drafted else "posted"), url, note
    return "skipped", "", f"platform {target['platform']} not automated"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="all")
    ap.add_argument("--account", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = config()
    state = load_state()
    clips = queue_items()
    if not clips:
        print("queue is empty - run the local producer and push new clips")
        return 0

    # an account not connected inside Whop's Content Rewards app can't have its posts submitted,
    # so posting from it would burn a clip for nothing
    targets = [t for t in cfg["targets"] if t.get("auto_post") and t.get("whop_linked")]
    skipped = [t["label"] for t in cfg["targets"] if t.get("auto_post") and not t.get("whop_linked")]
    if skipped:
        print("not linked in Content Rewards (skipped): " + ", ".join(skipped))
    if args.platform != "all":
        targets = [t for t in targets if t["platform"] == args.platform]
    if args.account:
        targets = [t for t in targets if t["id"] == args.account]

    exit_code = 0
    for t in targets:
        cadence = cfg.get("cadence", {}).get(t["platform"], {})
        cap = cadence.get("per_day", 3)
        state = refresh_state()
        done = posted_count(state, t["id"])
        if done >= cap:
            print(f"{t['label']}: daily cap reached ({done}/{cap})")
            continue
        clip = next_clip(state, t, clips)
        if not clip:
            print(f"{t['label']}: nothing queued for this account")
            continue
        try:
            status, url, note = post_clip(clip, t, cfg, args.dry_run)
        except Exception as err:
            status, url, note = "failed", "", f"{err.__class__.__name__}: {err}"[:400]
            exit_code = 1
        print(f"{t['label']}: {status} {clip['clip_id']} {url} {note}".rstrip())
        if args.dry_run:
            continue
        state["posts"].append({"clip_id": clip["clip_id"], "target_id": t["id"], "campaign_id": clip["campaign_id"],
                               "status": status, "url": url, "note": note, "at": now(), "submitted": False,
                               "_new": True})
        save_state(state)
        share_state(f"posted {clip['clip_id']} to {t['id']}")
        if status == "posted" and not url:  # e.g. TikTok published but its video link never showed up
            exit_code = 1
            unsubmitted_alert(clip, t, f"(no link found - open the latest post on {t['label']})",
                              "the platform didn't return the video link")

        # Whop pays only if the post is submitted inside the campaign's window (30 min on most campaigns)
        if status == "posted" and url and not args.dry_run:
            try:
                import whop_submit

                ok = whop_submit.submit_with_retry(clip, url, t)
                state["posts"][-1]["submitted"] = ok
                print(f"  whop submission: {'done' if ok else 'FAILED'}")
                if not ok:
                    exit_code = 1
                    print("  !! post is live but UNSUBMITTED - submit it by hand now, it earns nothing otherwise")
                    unsubmitted_alert(clip, t, url, "no confirmation from Whop")
            except Exception as err:
                print(f"  whop submission error: {err.__class__.__name__}: {err}")
                exit_code = 1
                unsubmitted_alert(clip, t, url, f"{err.__class__.__name__}: {str(err)[:200]}")
            save_state(state)
    if not args.dry_run:
        retire_finished(state, clips)
    return exit_code


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
