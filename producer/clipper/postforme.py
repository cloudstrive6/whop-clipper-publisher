"""Posting through Post for Me (TikTok drafts, X, and any other platform connected there).

TikTok's own API ignores captions on drafts, so the caption is written next to the clip and
printed for you to paste when you publish from the TikTok app.
"""
import json
import urllib.request
from pathlib import Path

from .config import cfg, path


def _api_key() -> str:
    p = path(cfg()["postforme"]["api_key_file"])
    if not p.exists():
        raise FileNotFoundError(f"Missing {p}. See SETUP.md step 6.")
    return p.read_text(encoding="utf-8").strip()


def client():
    from post_for_me import PostForMe

    return PostForMe(api_key=_api_key())


def accounts() -> list[dict]:
    """Connected accounts as [{id, platform, username, external_id}]."""
    out = []
    for a in client().social_accounts.list(limit=100).data:
        out.append({"id": a.id, "platform": a.platform, "username": a.username or "",
                    "external_id": a.external_id or ""})
    return out


def account_id(platform: str, handle: str, external_id: str | None = None) -> str:
    """Post for Me stores display names, not handles, so match the target's external_id first."""
    want = handle.lstrip("@").lower().replace(" ", "")
    found = accounts()
    for a in found:
        if a["platform"] == platform and external_id and a["external_id"] == external_id:
            return a["id"]
    for a in found:
        if a["platform"] == platform and a["username"].lower().replace(" ", "") == want:
            return a["id"]
    known = ", ".join(f"{a['platform']}:{a['username']} ({a['external_id']})" for a in found) or "none"
    raise RuntimeError(f"@{handle} is not connected to Post for Me as a {platform} account. Connected: {known}")


def upload(file: Path) -> str:
    """Uploads the clip and returns the media URL Post for Me will post from."""
    c = client()
    slot = c.media.create_upload_url()
    body = file.read_bytes()
    req = urllib.request.Request(slot.upload_url, data=body, method="PUT",
                                 headers={"Content-Type": "video/mp4"})
    with urllib.request.urlopen(req, timeout=900) as r:
        r.read()
    return slot.media_url


def post(file: Path, caption: str, platform: str, handle: str, *, draft: bool = True,
         sponsored: bool = False, external_id: str | None = None) -> tuple[str, str]:
    """Creates the post (a draft for TikTok). Returns (url_or_empty, note)."""
    c = client()
    acct = account_id(platform, handle, external_id)
    media_url = upload(file)
    config = {}
    if platform == "tiktok":
        config["tiktok"] = {
            "is_draft": draft,
            "privacy_status": "public",
            "disclose_branded_content": sponsored,   # campaign clips are paid partnerships
            "disclose_your_brand": False,
            "allow_comment": True,                   # several campaigns require comments left on
        }
    created = c.social_posts.create(caption=caption, social_accounts=[acct],
                                    media=[{"url": media_url}],
                                    platform_configurations=config or None)
    if platform == "tiktok" and draft:
        note = (f"draft sent to @{handle}'s TikTok. Open TikTok -> Profile -> Drafts, paste the caption "
                f"(TikTok drops captions on drafts), then publish. post id {created.id}")
    else:
        note = f"post id {created.id}"
    return "", note
