"""Download campaign source footage (YouTube / Google Drive / direct links)."""
import re
import subprocess
import sys
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def download(url: str, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    before = set(dest.rglob("*"))
    if "drive.google.com" in url:
        import gdown

        if "/folders/" in url:
            gdown.download_folder(url, output=str(dest), quiet=False, remaining_ok=True)
        else:
            gdown.download(url, output=str(dest) + "/", quiet=False, fuzzy=True)
    else:
        subprocess.run(
            [sys.executable, "-m", "yt_dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
             "--merge-output-format", "mp4", "-o", str(dest / "%(title).80s [%(id)s].%(ext)s"), url],
            check=True,
        )
    new = [p for p in set(dest.rglob("*")) - before if p.suffix.lower() in VIDEO_EXT]
    return sorted(new) or sorted(p for p in dest.rglob("*") if p.suffix.lower() in VIDEO_EXT)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]
