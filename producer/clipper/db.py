"""SQLite state: campaigns and clips move through statuses."""
import json
import sqlite3
from datetime import datetime, timezone

from .config import DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  id TEXT PRIMARY KEY,
  title TEXT,
  url TEXT,
  data TEXT,             -- raw scraped fields (json)
  score REAL,
  status TEXT,           -- discovered | shortlisted | applied | joined | analyzed | skipped
  checklist TEXT,        -- brief analysis (json)
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY,
  campaign_id TEXT,
  source TEXT,
  start REAL, end REAL,
  meta TEXT,             -- title, hook, description, tags (json)
  file TEXT,
  qa TEXT,               -- qa report (json)
  status TEXT,           -- rendered | qa_failed | approved | rejected | posted | submitted
  youtube_url TEXT,
  posted_at TEXT,
  submitted_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY,   -- clip_id + ':' + target_id
  clip_id TEXT,
  target_id TEXT,
  url TEXT,
  status TEXT,           -- pending | posted | draft_uploaded | manual_ready | submitted | failed
  note TEXT,
  posted_at TEXT,
  submitted_at TEXT,
  updated_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    c = sqlite3.connect(DATA / "state.db")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def upsert(table: str, row: dict) -> None:
    row = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in row.items()}
    row["updated_at"] = now()
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "id")
    with conn() as c:
        c.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}",
            list(row.values()),
        )


def rows(table: str, where: str = "1=1", *args) -> list[dict]:
    with conn() as c:
        out = []
        for r in c.execute(f"SELECT * FROM {table} WHERE {where}", args):
            d = dict(r)
            for k in ("data", "checklist", "meta", "qa"):
                if d.get(k):
                    d[k] = json.loads(d[k])
            out.append(d)
        return out


def get(table: str, id_: str) -> dict | None:
    r = rows(table, "id=?", id_)
    return r[0] if r else None
