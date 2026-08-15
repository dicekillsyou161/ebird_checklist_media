"""SQLite persistence for the bot: one file, bot.db, shared by every store.

This replaces the old aliases.json / subscriptions.json. Existing JSON files
are imported automatically on first start and renamed *.migrated, so nothing
is lost and nothing is imported twice.

Why a database rather than JSON files:
- writes are real transactions, so a crash mid-write can never truncate or
  half-update the state (the JSON files were rewritten whole every save);
- the file is created owner-readable only; it holds Discord user IDs and
  per-user watch lists, which are nobody else's business on a shared box;
- name lookups update just the rows that changed instead of serializing
  everything on every learned name.

sqlite3 is in Python's standard library; this adds no dependency.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_links (
    discord_id TEXT PRIMARY KEY,
    ebird_id   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS names (
    key      TEXT PRIMARY KEY,   -- lowercased display name
    ebird_id TEXT NOT NULL,
    display  TEXT NOT NULL       -- original casing, for showing back
);
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id       TEXT NOT NULL,  -- Discord user ID
    region        TEXT NOT NULL,  -- eBird region code, e.g. US-WA-033
    region_label  TEXT NOT NULL DEFAULT '',
    min_rarity    INTEGER NOT NULL DEFAULT 0,
    confirmations INTEGER NOT NULL DEFAULT 0,
    created       TEXT NOT NULL DEFAULT '',
    alerts_sent   INTEGER NOT NULL DEFAULT 0,
    last_alert    TEXT NOT NULL DEFAULT '',
    paused        INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, region)
);
CREATE TABLE IF NOT EXISTS seen (
    user_id TEXT NOT NULL,
    region  TEXT NOT NULL,
    key     TEXT NOT NULL,        -- checklist:species of a delivered report
    obs_dt  TEXT NOT NULL DEFAULT '',
    status  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, region, key),
    FOREIGN KEY (user_id, region)
        REFERENCES subscriptions (user_id, region) ON DELETE CASCADE
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the bot database, creating schema and tightening permissions."""
    path = Path(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # crash-safe without blocking reads
    conn.execute("PRAGMA foreign_keys=ON")   # removing a subscription drops its seen rows
    conn.executescript(SCHEMA)
    conn.commit()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # permissions are best-effort (e.g. odd filesystems)
    return conn
