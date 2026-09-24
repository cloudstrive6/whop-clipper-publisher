"""Instagram Reels publishing in CI (Instagram API with Instagram Login)."""
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://graph.instagram.com/v23.0"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v23.0"


def _creds(target: dict) -> tuple[str, str]:
    store = json.loads(os.environ.get("INSTAGRAM_TOKENS", "{}"))
    entry = store.get(target["handle"])
    if not entry:
        raise RuntimeError(f"no Instagram token for @{target['handle']} in INSTAGRAM_TOKENS secret")
    return entry["token"], entry["user_id"]


def _get(endpoint: str, token: str, params: dict) -> dict:
    q = urllib.parse.urlencode({**params, "access_token": token})
    with urllib.request.urlopen(f"{API}/{endpoint}?{q}", timeout=120) as r:
        return json.loads(r.read())


def _post(endpoint: str, token: str, params: dict) -> dict:
    data = urllib.parse.urlencode({**params, "access_token": token}).encode()
    with urllib.request.urlopen(urllib.request.Request(f"{API}/{endpoint}", data=data), timeout=300) as r:
        return json.loads(r.read())


def publish_reel(file: Path, caption: str, target: dict) -> str:
    token, user_id = _creds(target)
    c = _post(f"{user_id}/media", token, {"media_type": "REELS", "upload_type": "resumable", "caption": caption})
    container = c["id"]
    body = file.read_bytes()
    req = urllib.request.Request(f"{RUPLOAD}/{container}", data=body, method="POST", headers={
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(body))})
    with urllib.request.urlopen(req, timeout=900) as r:
        r.read()
    for _ in range(60):
        st = _get(container, token, {"fields": "status_code,status"})
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"Instagram processing failed: {st.get('status')}")
        time.sleep(10)
    else:
        raise TimeoutError("Instagram did not finish processing in 10 minutes")
    pub = _post(f"{user_id}/media_publish", token, {"creation_id": container})
    link = _get(pub["id"], token, {"fields": "permalink"})
    return link.get("permalink", f"https://www.instagram.com/reel/{pub['id']}/")
