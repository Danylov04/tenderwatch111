"""SQLite: схема, з'єднання, дрібні хелпери.

SQLite тут свідомо: один писач (збирач), тисячі рядків, бекап = копія файлу. WAL вмикається завжди.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .util import now_iso

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
  pk            INTEGER PRIMARY KEY AUTOINCREMENT,
  title         TEXT NOT NULL,
  title_norm    TEXT NOT NULL DEFAULT '',
  buyer         TEXT NOT NULL DEFAULT '',
  buyer_norm    TEXT NOT NULL DEFAULT '',
  buyer_nip     TEXT NOT NULL DEFAULT '',
  province      TEXT NOT NULL DEFAULT '',
  city          TEXT NOT NULL DEFAULT '',
  kind          TEXT NOT NULL DEFAULT '',
  category      TEXT NOT NULL DEFAULT '',
  tags          TEXT NOT NULL DEFAULT '[]',
  relevance     TEXT NOT NULL DEFAULT 'review',     -- relevant | review | noise
  score         REAL NOT NULL DEFAULT 0,
  reasons       TEXT NOT NULL DEFAULT '[]',
  lifecycle     TEXT NOT NULL DEFAULT 'unknown',    -- open | planned | awarded | closed | cancelled | unknown
  cpv_main      TEXT NOT NULL DEFAULT '',
  cpv_all       TEXT NOT NULL DEFAULT '[]',
  value_amount  REAL,
  value_currency TEXT NOT NULL DEFAULT '',
  deadline      TEXT,
  deadline_src  TEXT NOT NULL DEFAULT '',
  published_at  TEXT,
  last_notice_at TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  last_changed  TEXT NOT NULL,
  url           TEXT NOT NULL DEFAULT '',
  sources       TEXT NOT NULL DEFAULT '[]',
  lots          INTEGER,
  attempts      INTEGER NOT NULL DEFAULT 1,
  description   TEXT NOT NULL DEFAULT '',
  reference     TEXT NOT NULL DEFAULT '',
  contact_email TEXT NOT NULL DEFAULT '',
  contractors   TEXT NOT NULL DEFAULT '[]',
  links         TEXT NOT NULL DEFAULT '{}',         -- {source: url}
  user_status   TEXT NOT NULL DEFAULT 'new',        -- new | watch | applied | skip
  user_note     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tenders_rel ON tenders(relevance, lifecycle, deadline);
CREATE INDEX IF NOT EXISTS idx_tenders_buyer ON tenders(buyer_norm);

CREATE TABLE IF NOT EXISTS tender_keys (
  key       TEXT PRIMARY KEY,
  tender_pk INTEGER NOT NULL REFERENCES tenders(pk) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_keys_pk ON tender_keys(tender_pk);

CREATE TABLE IF NOT EXISTS notices (
  pk           INTEGER PRIMARY KEY AUTOINCREMENT,
  tender_pk    INTEGER NOT NULL REFERENCES tenders(pk) ON DELETE CASCADE,
  source       TEXT NOT NULL,
  source_id    TEXT NOT NULL,
  notice_type  TEXT NOT NULL DEFAULT '',
  published_at TEXT,
  url          TEXT NOT NULL DEFAULT '',
  first_seen   TEXT NOT NULL,
  UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_notices_pk ON notices(tender_pk);

-- сирі знімки: новий рядок лише коли змінився хеш вмісту (повна історія змін джерела)
CREATE TABLE IF NOT EXISTS snapshots (
  pk        INTEGER PRIMARY KEY AUTOINCREMENT,
  tender_pk INTEGER NOT NULL REFERENCES tenders(pk) ON DELETE CASCADE,
  source    TEXT NOT NULL,
  source_id TEXT NOT NULL,
  seen_at   TEXT NOT NULL,
  hash      TEXT NOT NULL,
  raw       TEXT NOT NULL,
  UNIQUE(source, source_id, hash)
);

-- все, що переглянули (навіть шум зі sweep) — щоб не тягнути тіло двічі і мати аудит «чому не знайшли»
CREATE TABLE IF NOT EXISTS seen_notices (
  source       TEXT NOT NULL,
  source_id    TEXT NOT NULL,
  verdict      TEXT NOT NULL,
  score        REAL NOT NULL DEFAULT 0,
  body_scanned INTEGER NOT NULL DEFAULT 0,
  seen_at      TEXT NOT NULL,
  PRIMARY KEY (source, source_id)
);

-- яка стратегія/джерело знайшли тендер (основа для оцінки повноти capture-recapture)
CREATE TABLE IF NOT EXISTS hits (
  tender_pk INTEGER NOT NULL REFERENCES tenders(pk) ON DELETE CASCADE,
  source    TEXT NOT NULL,
  strategy  TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  PRIMARY KEY (tender_pk, source, strategy)
);

CREATE TABLE IF NOT EXISTS events (
  pk        INTEGER PRIMARY KEY AUTOINCREMENT,
  tender_pk INTEGER NOT NULL REFERENCES tenders(pk) ON DELETE CASCADE,
  at        TEXT NOT NULL,
  kind      TEXT NOT NULL,
  detail    TEXT NOT NULL DEFAULT '{}',
  notified  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_notified ON events(notified, at);

CREATE TABLE IF NOT EXISTS runs (
  pk          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,
  mode        TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  ok          INTEGER NOT NULL DEFAULT 0,
  fetched     INTEGER NOT NULL DEFAULT 0,
  relevant    INTEGER NOT NULL DEFAULT 0,
  new_tenders INTEGER NOT NULL DEFAULT 0,
  error       TEXT NOT NULL DEFAULT '',
  detail      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runs_src ON runs(source, mode, started_at);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path: str, init: bool = True, check_same_thread: bool = True) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=check_same_thread)  # autocommit; транзакції — явно
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if init:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        if kv_get(conn, "schema_version") is None:
            kv_set(conn, "schema_version", str(SCHEMA_VERSION))
    return conn


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
    )


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def jload(text: str | None, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def add_event(conn: sqlite3.Connection, tender_pk: int, kind: str, detail: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO events(tender_pk, at, kind, detail) VALUES(?,?,?,?)",
        (tender_pk, now_iso(), kind, jdump(detail or {})),
    )
    return int(cur.lastrowid or 0)
