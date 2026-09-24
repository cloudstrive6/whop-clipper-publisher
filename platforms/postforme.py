"""TikTok / X posting through Post for Me in CI.

TikTok: tries a direct public post first (Post for Me is an audited TikTok partner). If the API
rejects direct posting, it falls back to a draft, which you publish from the TikTok app.
"""
import os
import urllib.request
from pathlib import Path


def _client():
    from post_for_me import PostForMe

    key = os.environ.get("POSTFORME_API_KEY")
    if not key:
        raise RuntimeError("missing secret POSTFORME_API_KEY")
    return PostForMe(api_key=key)


def account_id(client, platform: str, target: dict) -> str:
    want_ext = target.get("postforme_external_id")
    want_name = target["handle"].lstrip("@").lower().replace(" ", "")
    accounts = client.social_accounts.list(limit=100).data
    for a in accounts:
        if a.platform == platform and want_ext and a.external_id == want_ext:
            return a.id
    for a in accounts:
        if a.platform == platform and (a.username or "").lower().replace(" ", "") == want_name:
            return a.id
    raise RuntimeError(f"@{target['handle']} not connected to Post for Me as {platform}")


def upload(client, file: Path) -> str:
    slot = client.media.create_upload_url()
    req = urllib.request.Request(slot.upload_url, data=file.read_bytes(), method="PUT",
                                 headers={"Content-Type": "video/mp4"})
    with urllib.request.urlopen(req, timeout=900) as r:
        r.read()
    return slot.media_url


def post(file: Path, caption: str, target: dict, *, sponsored: bool = True) -> tuple[str, str, bool]:
    """Returns (url, note, is_draft)."""
    c = _client()
    platform = target["platform"]
    acct = account_id(c, platform, target)
    media_url = upload(c, file)

    def create(as_draft: bool):
        config = {}
        if platform == "tiktok":
            config["tiktok"] = {"is_draft": as_draft, "privacy_status": "public",
                                "disclose_branded_content": sponsored, "disclose_your_brand": False,
                                "allow_comment": True}
        return c.social_posts.create(caption=caption, social_accounts=[acct],
                                     media=[{"url": media_url}], platform_configurations=config or None)

    if platform != "tiktok":
        created = create(False)
        return "", f"post id {created.id}", False
    try:
        created = create(False)  # direct public post
        return "", f"post id {created.id} (direct)", False
    except Exception as err:
        created = create(True)   # fall back to a draft
        return "", (f"direct post refused ({err.__class__.__name__}); sent as draft to @{target['handle']} "
                    f"- publish it in the TikTok app with the saved caption. post id {created.id}"), True
