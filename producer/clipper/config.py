import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IN_CI = bool(os.environ.get("GITHUB_ACTIONS"))


def pw_channel() -> str | None:
    """Installed Chrome locally; Playwright's bundled Chromium on GitHub runners."""
    return None if IN_CI else "chrome"


@lru_cache
def cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def path(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p


def campaign_dir(campaign_id: str) -> Path:
    d = DATA / "campaigns" / campaign_id
    d.mkdir(parents=True, exist_ok=True)
    return d
