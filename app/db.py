"""SQLite storage. One file, WAL mode, a single process-wide lock around writes."""
import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterable

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS plays (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  config TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, last_run_at REAL
);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, play_id INTEGER NOT NULL, lead_id INTEGER,
  source TEXT NOT NULL, type TEXT NOT NULL, title TEXT, text TEXT, url TEXT,
  occurred_at REAL NOT NULL, hash TEXT NOT NULL, extra TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL, UNIQUE(play_id, hash)
);
CREATE INDEX IF NOT EXISTS signals_lead ON signals(lead_id);

CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY, play_id INTEGER NOT NULL, lead_key TEXT NOT NULL,
  company_name TEXT, company_domain TEXT, description TEXT,
  linkedin_company_url TEXT, tech TEXT NOT NULL DEFAULT '[]',
  score REAL NOT NULL DEFAULT 0, fit REAL NOT NULL DEFAULT 0, intent REAL NOT NULL DEFAULT 0,
  recency REAL NOT NULL DEFAULT 0, reasons TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'new', enriched_at REAL, note TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(play_id, lead_key)
);
CREATE INDEX IF NOT EXISTS leads_status ON leads(play_id, status, score);

CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL, name TEXT, title TEXT,
  email TEXT, email_status TEXT, email_detail TEXT NOT NULL DEFAULT '{}',
  phone TEXT, phone_detail TEXT NOT NULL DEFAULT '{}',
  linkedin_url TEXT, source TEXT, is_primary INTEGER NOT NULL DEFAULT 0,
  do_not_contact INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS contacts_lead ON contacts(lead_id);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL, contact_id INTEGER,
  channel TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 1, subject TEXT, body TEXT,
  status TEXT NOT NULL DEFAULT 'draft', scheduled_at REAL, sent_at REAL,
  outcome TEXT, error TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_due ON messages(channel, status, scheduled_at);

CREATE TABLE IF NOT EXISTS replies (
  id INTEGER PRIMARY KEY, lead_id INTEGER, contact_id INTEGER, from_addr TEXT,
  subject TEXT, body TEXT, received_at REAL NOT NULL, kind TEXT NOT NULL DEFAULT 'reply',
  draft_reply TEXT, handled INTEGER NOT NULL DEFAULT 0, message_id TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS suppression (
  value TEXT PRIMARY KEY, reason TEXT, created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, play_id INTEGER, kind TEXT NOT NULL DEFAULT 'play',
  started_at REAL NOT NULL, finished_at REAL, stats TEXT NOT NULL DEFAULT '{}', error TEXT
);

CREATE TABLE IF NOT EXISTS seo_audits (
  id INTEGER PRIMARY KEY, url TEXT NOT NULL, created_at REAL NOT NULL, report TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL NOT NULL
);
"""

JSON_COLS = {"config", "extra", "tech", "reasons", "email_detail", "phone_detail", "stats", "report"}


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=5000")
            _conn.executescript(SCHEMA)
            # Columns added after the first release; safe to run on every start.
            have = {r[1] for r in _conn.execute("PRAGMA table_info(leads)").fetchall()}
            if "variant" not in have:
                _conn.execute("ALTER TABLE leads ADD COLUMN variant TEXT")
        return _conn


def reset_for_tests(path: str) -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        config.DB_PATH = path


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in list(d):
        if k in JSON_COLS and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def q(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        return [_row(r) for r in conn().execute(sql, tuple(params)).fetchall()]


def one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def x(sql: str, params: Iterable[Any] = ()) -> int:
    """Execute a write; returns lastrowid (or rowcount for UPDATE/DELETE)."""
    with _lock:
        cur = conn().execute(sql, tuple(params))
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount


def j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def now() -> float:
    return time.time()


def get_setting(key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def set_setting(key: str, value: Any) -> None:
    x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, j(value)))


def cache_get(key: str, max_age: float) -> Any:
    row = one("SELECT value, created_at FROM cache WHERE key=?", (key,))
    if row and now() - row["created_at"] < max_age:
        try:
            return json.loads(row["value"])
        except ValueError:
            return None
    return None


def cache_set(key: str, value: Any) -> None:
    x("INSERT INTO cache(key,value,created_at) VALUES(?,?,?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value, created_at=excluded.created_at",
      (key, j(value), now()))
