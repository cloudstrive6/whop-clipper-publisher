"""Posting targets: match campaigns to accounts, and publish to each platform."""
from pathlib import Path

from . import db
from .config import cfg


def all_targets() -> list[dict]:
    return cfg().get("targets", [])


def get(target_id: str) -> dict | None:
    return next((t for t in all_targets() if t["id"] == target_id), None)


def for_campaign(campaign: dict) -> list[dict]:
    """Targets whose niche matches the campaign and whose platform the campaign accepts."""
    data = campaign.get("data") or {}
    ck = campaign.get("checklist") or {}
    niche = (ck.get("niche") or data.get("niche") or "").lower()
    if not niche:
        print(f"[targets] {campaign.get('title')}: no niche recorded, run analyze first")
        return []
    allowed = [p.lower() for p in ck.get("allowed_platforms", [])]
    out = []
    for t in all_targets():
        if not t.get("auto_post", False) and t["platform"] != "manual":
            continue
        if niche not in [n.lower() for n in t.get("niches", [])]:
            continue
        p = t["platform"]
        platform_name = {"manual": t.get("handle_platform", "x")}.get(p, p)
        if allowed and platform_name not in allowed:
            continue  # the brief restricts platforms
        if p == "youtube" and ck.get("eligible_for_youtube") is False:
            continue
        out.append(t)
    return out


def plan(clip: dict, campaign: dict) -> list[dict]:
    """Create pending post rows for a clip across its campaign's targets."""
    rows = []
    for t in for_campaign(campaign):
        pid = f"{clip['id']}:{t['id']}"
        if not db.get("posts", pid):
            db.upsert("posts", {"id": pid, "clip_id": clip["id"], "target_id": t["id"], "status": "pending"})
        rows.append(db.get("posts", pid))
    return rows


def campaign_of(clip: dict) -> dict | None:
    return db.get("campaigns", clip["campaign_id"])


def _caption_file(file: Path, target: dict, caption: str) -> Path:
    """TikTok drafts lose the caption, so keep it next to the clip, ready to paste."""
    p = file.with_name(f"{file.stem}.{target['id']}.caption.txt")
    p.write_text(caption, encoding="utf-8")
    return p


def publish_one(post: dict, clip: dict) -> dict:
    """Post to one target. Returns the updated row."""
    t = get(post["target_id"])
    meta = clip["meta"]
    desc = f"{meta['description']}\n\n{' '.join(meta['hashtags'])}"
    file = Path(clip["file"])
    try:
        if t["platform"] == "youtube":
            from . import youtube

            url = youtube.upload(file, meta["title"], desc, meta["hashtags"], token=t.get("token"))
            status, note = "posted", ""
        elif t["platform"] == "instagram":
            from . import instagram

            instagram.refresh_tokens()
            url = instagram.publish_reel(file, desc, t)
            status, note = "posted", ""
        elif t["platform"] in ("tiktok", "x"):
            from . import postforme

            caption = f"{meta['title']}\n\n{desc}"
            _caption_file(file, t, caption)
            sponsored = bool((campaign_of(clip) or {}).get("checklist", {}).get("required_in_caption"))
            url, note = postforme.post(file, caption, t["platform"], t["handle"],
                                       draft=(t["platform"] == "tiktok"), sponsored=sponsored,
                                       external_id=t.get("postforme_external_id"))
            status = "draft_uploaded" if t["platform"] == "tiktok" else "posted"
        else:  # manual
            url, status = "", "manual_ready"
            caption = f"{meta['title']}\n\n{desc}"
            note = f"Post {file.name} manually. Caption saved to {_caption_file(file, t, caption).name}"
    except Exception as err:
        db.upsert("posts", {"id": post["id"], "status": "failed", "note": f"{err.__class__.__name__}: {err}"[:500]})
        raise
    db.upsert("posts", {"id": post["id"], "url": url, "status": status, "note": note[:500],
                        "posted_at": db.now()})
    return db.get("posts", post["id"])
