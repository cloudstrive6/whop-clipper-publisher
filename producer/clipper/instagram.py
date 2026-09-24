"""Instagram Reels publishing via the Instagram API with Instagram Login.

Each account has its own long-lived access token (generated in the Meta app under
Use cases -> Instagram -> API setup with Instagram login -> Generate access tokens).
Tokens live in secrets/instagram_tokens.json as {"<handle>": {"token": "...", "user_id": "..."}}.
"""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .config import cfg, path

API = "https://graph.instagram.com/v23.0"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v23.0"


def _store() -> dict:
    p = path(cfg()["instagram"]["token"])
    if not p.exists():
        raise FileNotFoundError(f"Missing {p}. See SETUP.md step 5.")
    return json.loads(p.read_text(encoding="utf-8"))


def creds(target: dict) -> tuple[str, str]:
    entry = _store().get(target["handle"])
    if not entry:
        raise RuntimeError(f"No Instagram token stored for @{target['handle']} (see SETUP.md step 5)")
    return entry["token"], entry["user_id"]


def refresh_tokens(min_age_days: int = 7) -> None:
    """Instagram long-lived tokens last 60 days; refreshing an unexpired one resets the clock."""
    import datetime

    p = path(cfg()["instagram"]["token"])
    store = _store()
    changed = False
    for handle, entry in store.items():
        last = entry.get("refreshed_at")
        if last:
            age = (datetime.datetime.now(datetime.timezone.utc)
                   - datetime.datetime.fromisoformat(last)).days
            if age < min_age_days:
                continue
        try:
            q = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": entry["token"]})
            with urllib.request.urlopen(f"https://graph.instagram.com/refresh_access_token?{q}", timeout=60) as r:
                data = json.loads(r.read())
            entry["token"] = data["access_token"]
            entry["refreshed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            entry["expires_in_days"] = round(data.get("expires_in", 0) / 86400)
            changed = True
            print(f"[instagram] refreshed @{handle} (valid ~{entry['expires_in_days']} more days)")
        except Exception as err:
            print(f"[instagram] could not refresh @{handle}: {err.__class__.__name__} "
                  f"- regenerate it in the Meta app if publishing fails")
    if changed:
        p.write_text(json.dumps(store, indent=2), encoding="utf-8")


def _get(endpoint: str, token: str, params: dict) -> dict:
    q = urllib.parse.urlencode({**params, "access_token": token})
    with urllib.request.urlopen(f"{API}/{endpoint}?{q}", timeout=120) as r:
        return json.loads(r.read())


def _post(endpoint: str, token: str, params: dict) -> dict:
    data = urllib.parse.urlencode({**params, "access_token": token}).encode()
    with urllib.request.urlopen(urllib.request.Request(f"{API}/{endpoint}", data=data), timeout=300) as r:
        return json.loads(r.read())


def whoami(target: dict) -> dict:
    token, _ = creds(target)
    return _get("me", token, {"fields": "user_id,username,account_type,media_count"})


def publish_reel(file: Path, caption: str, target: dict) -> str:
    token, user_id = creds(target)
    # 1. container for a resumable upload
    c = _post(f"{user_id}/media", token, {"media_type": "REELS", "upload_type": "resumable", "caption": caption})
    container = c["id"]
    # 2. upload the bytes
    body = file.read_bytes()
    req = urllib.request.Request(f"{RUPLOAD}/{container}", data=body, method="POST", headers={
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(body)),
    })
    with urllib.request.urlopen(req, timeout=900) as r:
        r.read()
    # 3. wait for processing, then publish
    for _ in range(60):
        st = _get(container, token, {"fields": "status_code,status"})
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"Instagram processing failed: {st.get('status')}")
        time.sleep(10)
    else:
        raise TimeoutError("Instagram did not finish processing the reel in 10 minutes")
    pub = _post(f"{user_id}/media_publish", token, {"creation_id": container})
    link = _get(pub["id"], token, {"fields": "permalink"})
    return link.get("permalink", f"https://www.instagram.com/reel/{pub['id']}/")
