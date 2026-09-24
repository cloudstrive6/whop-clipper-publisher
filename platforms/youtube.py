"""YouTube upload in CI. Tokens arrive as env vars (GitHub Secrets), not files."""
import json
import os
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def _service(target: dict):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    env = target.get("token_env") or f"YOUTUBE_TOKEN_{target['id'].upper()}"
    raw = os.environ.get(env)
    if not raw:
        raise RuntimeError(f"missing secret {env}")
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload(file: Path, title: str, description: str, tags: list[str], target: dict, cfg: dict) -> str:
    from googleapiclient.http import MediaFileUpload

    y = cfg.get("youtube", {})
    body = {
        "snippet": {"title": title[:100], "description": description[:4900],
                    "tags": [t.lstrip("#") for t in tags][:15], "categoryId": y.get("category_id", "24")},
        "status": {"privacyStatus": y.get("privacy_on_upload", "public"), "selfDeclaredMadeForKids": False},
    }
    parts = "snippet,status"
    if y.get("geo_location"):
        body["recordingDetails"] = {"locationDescription": y["geo_location"]}
        parts += ",recordingDetails"
    req = _service(target).videos().insert(part=parts, body=body,
                                           media_body=MediaFileUpload(str(file), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return f"https://www.youtube.com/shorts/{resp['id']}"
