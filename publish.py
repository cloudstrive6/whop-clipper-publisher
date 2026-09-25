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
               if p["target_id"] == target_id and p["status"] in ("posted", "draft_uploaded", "posting")
               and datetime.fromisoformat(p["at"]) > cutoff)


def already_posted(state: dict, clip_id: str, target_id: str) -> bool:
    return any(p["clip_id"] == clip_id and p["target_id"] == target_id
               and p["status"] in ("posted", "draft_uploaded", "posting") for p in state["posts"])


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


UNAVAILABLE = ROOT / "state" / "unavailable.json"  # campaigns Whop can't take submissions for, and since when
RECHECK_HOURS, RETIRE_HOURS = 2, 24


def campaign_ready(clip: dict, cache: dict, clips: list[dict], state: dict) -> str | None:
    """None if the clip's campaign can take a submission right now, else why not.

    Guard 2: only post what can be submitted. The Submit dialog is opened (and cancelled) before posting.
    A campaign that fails is rechecked every 2 hours, not every slot; after 24 hours unavailable it is
    treated as ended and its queued clips are removed."""
    import whop_submit
    from notify import alert, once

    cid = clip["campaign_id"]
    if cid in cache:
        return cache[cid]
    ledger = json.loads(UNAVAILABLE.read_text(encoding="utf-8")) if UNAVAILABLE.exists() else {}
    entry = ledger.get(cid)
    t_now = datetime.now(timezone.utc)
    if entry and t_now - datetime.fromisoformat(entry["checked"]) < timedelta(hours=RECHECK_HOURS):
        cache[cid] = entry["why"]
        return entry["why"]
    why = whop_submit.preflight(clip)
    cache[cid] = why
    if why is None:
        if entry:
            ledger.pop(cid)
            alert(f"▶️ {clip['campaign_title']} is taking submissions again", "Its clips are posting again.")
    else:
        entry = entry or {"since": t_now.isoformat(timespec="seconds")}
        entry.update(checked=t_now.isoformat(timespec="seconds"), why=why)
        ledger[cid] = entry
        hours = (t_now - datetime.fromisoformat(entry["since"])).total_seconds() / 3600
        if hours >= RETIRE_HOURS:
            gone = [c for c in clips if c["campaign_id"] == cid]
            for c in gone:
                if c.get("video_asset"):
                    subprocess.run(["gh", "release", "delete-asset", RELEASE, c["video_asset"], "-y"], cwd=ROOT)
                shutil.rmtree(c["dir"], ignore_errors=True)
            clips[:] = [c for c in clips if c["campaign_id"] != cid]
            ledger.pop(cid)
            alert(f"🛑 {clip['campaign_title']} looks ended - {len(gone)} queued clip(s) removed",
                  f"Whop hasn't taken submissions for it for {hours:.0f} hours ({why}). Its slots go to other campaigns.")
        else:
            once(f"unavailable|{cid}|{entry['since']}", f"⏸ {clip['campaign_title']} can't take submissions right now",
                 f"Reason: {why}\n\nNothing of it gets posted, so nothing needs submitting by hand. Other campaigns' "
                 f"clips fill those slots meanwhile. Rechecked every {RECHECK_HOURS}h; if it's still unavailable after "
                 f"{RETIRE_HOURS}h it's treated as ended and its queued clips are removed.")
    UNAVAILABLE.parent.mkdir(parents=True, exist_ok=True)
    UNAVAILABLE.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    share_state(f"campaign availability: {cid}")
    return why


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
    whop_ready: dict[str, str | None] = {}  # campaign -> None if submittable, else why not
    for t in targets:
        cadence = cfg.get("cadence", {}).get(t["platform"], {})
        cap = t.get("per_day", cadence.get("per_day", 3))  # an account can have its own daily limit
        state = refresh_state()
        done = posted_count(state, t["id"])
        if done >= cap:
            print(f"{t['label']}: daily cap reached ({done}/{cap})")
            continue
        # pick the best clip whose campaign can take submissions right now; a campaign that can't
        # (paused, ended, Whop hiccup) is skipped and the next campaign's clip fills the slot
        unusable: dict[str, str] = {}
        clip = None
        while True:
            clip = next_clip(state, t, [c for c in clips if c["campaign_id"] not in unusable])
            if not clip or args.dry_run:
                break
            why = campaign_ready(clip, whop_ready, clips, state)
            if why is None:
                break
            unusable[clip["campaign_id"]] = why
            print(f"{t['label']}: skipping {clip['campaign_title']} - {why}")
        if not clip:
            if unusable:  # held on purpose (you were told on Telegram): not a failed run
                print(f"{t['label']}: no queued campaign can take submissions right now - nothing posted this slot")
            else:
                print(f"{t['label']}: nothing queued for this account")
            continue
        if args.dry_run:
            print(f"{t['label']}: would post {clip['clip_id']}")
            continue

        # guard 1: never twice - ask the platform itself whether this clip is already on the account
        caption = with_mentions(clip["caption"], clip, t["platform"])
        try:
            import guards

            found = guards.already_on_account(t, clip, caption)
        except Exception as err:
            found = None
            print(f"  (couldn't check the account for duplicates: {err.__class__.__name__})")
        if found:
            url, at = found
            print(f"{t['label']}: {clip['clip_id']} is already on the account ({url}) - not posting it again")
            state["posts"].append({"clip_id": clip["clip_id"], "target_id": t["id"], "campaign_id": clip["campaign_id"],
                                   "status": "posted", "url": url, "note": "found on the account", "_new": True,
                                   "at": at.astimezone(timezone.utc).isoformat(timespec="seconds"), "submitted": False})
            save_state(state)
            share_state(f"record existing post of {clip['clip_id']} on {t['id']}")
            continue

        # guard 3: claim the slot in the repo before uploading, so an overlapping run skips this clip
        state["posts"].append({"clip_id": clip["clip_id"], "target_id": t["id"], "campaign_id": clip["campaign_id"],
                               "status": "posting", "url": "", "note": "", "at": now(), "submitted": False,
                               "_new": True})
        save_state(state)
        share_state(f"posting {clip['clip_id']} to {t['id']}")
        try:
            status, url, note = post_clip(clip, t, cfg, args.dry_run)
        except Exception as err:
            status, url, note = "failed", "", f"{err.__class__.__name__}: {err}"[:400]
            exit_code = 1
        print(f"{t['label']}: {status} {clip['clip_id']} {url} {note}".rstrip())
        state["posts"][-1].update(status=status, url=url, note=note, at=now())
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
