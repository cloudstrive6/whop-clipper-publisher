"""Telegram alerts (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID secrets). Falls back to a GitHub issue.

Same as publisher/notify.py; this copy keeps its ledger in data/notified.json (committed with the run).
"""
import json
import os
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path


def telegram(text: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception as err:  # never let an alert failure break a run; the error text has no token in it
        print(f"::warning::Telegram alert failed: {err.__class__.__name__}")
        return False


def alert(title: str, body: str = "") -> None:
    """Telegram first; a GitHub issue (emailed) if Telegram isn't set up or fails."""
    if telegram(f"{title}\n\n{body}".strip()):
        return
    if not os.environ.get("GITHUB_ACTIONS"):
        print(f"ALERT: {title}\n{body}")
        return
    gh = {**os.environ, "GH_TOKEN": os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")}
    existing = subprocess.run(["gh", "issue", "list", "--state", "open", "--search", title, "--json", "title"],
                              capture_output=True, text=True, env=gh).stdout
    if title not in existing:
        subprocess.run(["gh", "issue", "create", "--title", title, "--body", body or title], env=gh)


def once(key: str, title: str, body: str = "", ledger: Path | None = None) -> bool:
    """Alert only the first time `key` is seen (ledger is committed with the run's state)."""
    ledger = ledger or Path(__file__).parent.parent / "data" / "notified.json"
    seen = json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else []
    if key in seen:
        return False
    alert(title, body)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps(seen + [key], indent=1), encoding="utf-8")
    return True
