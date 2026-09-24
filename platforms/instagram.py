"""Instagram Reels publishing in CI (Instagram API with Instagram Login).

This API variant can't take an uploaded file: it needs a public `video_url` to fetch the reel from.
The clip is parked in Post for Me's media storage for that, which hands back a public URL.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://graph.instagram.com/v23.0"


def _creds(target: dict) -> tuple[str, str]:
    store = json.loads(os.environ.get("INSTAGRAM_TOKENS", "{}"))
    entry = store.get(target["handle"])
    if not entry:
        raise RuntimeError(f"no Instagram token for @{target['handle']} in INSTAGRAM_TOKENS secret")
    return entry["token"], entry["user_id"]


def _call(method: str, endpoint: str, token: str, params: dict) -> dict:
    data = urllib.parse.urlencode({**params, "access_token": token})
    if method == "GET":
        req = urllib.request.Request(f"{API}/{endpoint}?{data}")
    else:
        req = urllib.request.Request(f"{API}/{endpoint}", data=data.encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as err:  # surface Meta's own message, not just "400"
        detail = err.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"Instagram {endpoint} HTTP {err.code}: {detail}") from None


def public_url(file: Path) -> str:
    from platforms import postforme

    return postforme.upload(postforme._client(), file)


def publish_reel(file: Path, caption: str, target: dict) -> str:
    token, user_id = _creds(target)
    video_url = public_url(file)
    c = _call("POST", f"{user_id}/media", token,
              {"media_type": "REELS", "video_url": video_url, "caption": caption, "share_to_feed": "true"})
    container = c["id"]
    for _ in range(60):
        st = _call("GET", container, token, {"fields": "status_code,status"})
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"Instagram processing failed: {st.get('status')}")
        time.sleep(10)
    else:
        raise TimeoutError("Instagram did not finish processing in 10 minutes")
    pub = _call("POST", f"{user_id}/media_publish", token, {"creation_id": container})
    link = _call("GET", pub["id"], token, {"fields": "permalink"})
    return link.get("permalink", f"https://www.instagram.com/reel/{pub['id']}/")
