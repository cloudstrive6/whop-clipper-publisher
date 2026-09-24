"""Cloud publisher: post one queued clip per account, then submit it on Whop.

Runs on GitHub Actions, fired by cron-job.org (one call per time slot). No GPU, no AI, no rendering:
everything creative was decided locally and approved before the clip entered the queue.

  python publish.py --platform youtube          # every account on that platform, one clip each
  python publish.py --platform tiktok --account tt_cryptohustler
  python publish.py --platform all --dry-run
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
QUEUE = ROOT / "queue"
STATE = ROOT / "state" / "posts.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"posts": []}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


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


def post_clip(clip: dict, target: dict, cfg: dict, dry: bool) -> tuple[str, str, str]:
    """Returns (status, url, note)."""
    caption = clip["caption"]
    if dry:
        return "dry_run", "", f"would post {clip['clip_id']} to {target['label']}"
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

    targets = [t for t in cfg["targets"] if t.get("auto_post")]
    if args.platform != "all":
        targets = [t for t in targets if t["platform"] == args.platform]
    if args.account:
        targets = [t for t in targets if t["id"] == args.account]

    exit_code = 0
    for t in targets:
        cadence = cfg.get("cadence", {}).get(t["platform"], {})
        cap = cadence.get("per_day", 3)
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
        state["posts"].append({"clip_id": clip["clip_id"], "target_id": t["id"], "campaign_id": clip["campaign_id"],
                               "status": status, "url": url, "note": note, "at": now(), "submitted": False})
        save_state(state)

        # Whop pays only if the post is submitted inside the campaign's window (often 1h, sometimes 10min)
        if status == "posted" and url and not args.dry_run:
            try:
                import whop_submit

                ok = whop_submit.submit(clip, url, t)
                state["posts"][-1]["submitted"] = ok
                print(f"  whop submission: {'done' if ok else 'FAILED'}")
                if not ok:
                    exit_code = 1
            except Exception as err:
                print(f"  whop submission error: {err.__class__.__name__}: {err}")
                exit_code = 1
            save_state(state)
    return exit_code


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
