"""YouTube Data API v3 uploader (OAuth, your own Google Cloud project)."""
from pathlib import Path

from .config import cfg, path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def service(token_path: str | None = None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    y = cfg()["youtube"]
    token, secret = path(token_path or y["token"]), path(y["client_secret"])
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not secret.exists():
                raise FileNotFoundError(f"Missing {secret}. See SETUP.md step 3.")
            creds = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES).run_local_server(port=0)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def whoami(token_path: str | None = None) -> str:
    r = service(token_path).channels().list(part="snippet", mine=True).execute()
    items = r.get("items", [])
    return f"{items[0]['snippet']['title']} ({items[0]['id']})" if items else "no channel"


def upload(file: Path, title: str, description: str, tags: list[str], token: str | None = None) -> str:
    from googleapiclient.http import MediaFileUpload

    y = cfg()["youtube"]
    body = {
        "snippet": {"title": title[:100], "description": description[:4900],
                    "tags": [t.lstrip("#") for t in tags][:15], "categoryId": y["category_id"]},
        "status": {"privacyStatus": y["privacy_on_upload"], "selfDeclaredMadeForKids": False},
    }
    if y.get("geo_location"):  # campaigns that ask for a geo-tag: YouTube's recording location
        body["recordingDetails"] = {"locationDescription": y["geo_location"]}
    parts = "snippet,status" + (",recordingDetails" if "recordingDetails" in body else "")
    req = service(token).videos().insert(part=parts, body=body,
                                     media_body=MediaFileUpload(str(file), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return f"https://www.youtube.com/shorts/{resp['id']}"
