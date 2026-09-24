"""Safety checks before anything is posted.

1. already_on_account: is this clip already live on the account? Asks the platform itself, so it works
   even if another run's records haven't arrived yet. A clip is never posted twice.
2. whop ready (whop_submit.preflight): the campaign's Submit dialog opens right now. If it doesn't, the
   clip isn't posted this slot: a post that can't be submitted earns nothing and would need you to
   submit it by hand within 30 minutes.
"""
from datetime import datetime, timezone


def _same(a: str, b: str) -> bool:
    norm = lambda s: " ".join((s or "").split()).lower()[:60]
    return bool(norm(a)) and norm(a) == norm(b)


def already_on_account(target: dict, clip: dict, caption: str) -> tuple[str, datetime] | None:
    """(url, posted_at) if the account already has this clip among its latest posts."""
    platform = target["platform"]
    if platform == "youtube":
        import platforms.youtube as yt

        svc = yt._service(target)
        up = svc.channels().list(part="contentDetails", mine=True).execute()["items"][0]
        playlist = up["contentDetails"]["relatedPlaylists"]["uploads"]
        for it in svc.playlistItems().list(part="snippet", playlistId=playlist, maxResults=10).execute()["items"]:
            s = it["snippet"]
            if _same(s["title"], clip["title"]):
                return (f"https://www.youtube.com/shorts/{s['resourceId']['videoId']}",
                        datetime.fromisoformat(s["publishedAt"].replace("Z", "+00:00")))
    elif platform == "instagram":
        import platforms.instagram as ig

        token, _ = ig._creds(target)
        media = ig._call("GET", "me/media", token, {"fields": "caption,permalink,timestamp", "limit": 10})
        for m in media.get("data", []):
            if _same(m.get("caption", ""), caption):
                return m["permalink"], datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
    elif platform == "tiktok":
        import platforms.postforme as pfm

        c = pfm._client()
        acct = pfm.account_id(c, "tiktok", target)
        for item in c.social_account_feeds.list(acct, limit=10).data:
            if _same(getattr(item, "caption", ""), caption) and getattr(item, "platform_url", None):
                at = getattr(item, "posted_at", None) or datetime.now(timezone.utc)
                at = at if isinstance(at, datetime) else datetime.fromisoformat(str(at))
                return item.platform_url.split("?")[0], at
    return None
