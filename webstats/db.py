"""SQLite schema and connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .config import Config


SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    source_fingerprint TEXT NOT NULL,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    ts INTEGER NOT NULL,
    day TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    query TEXT,
    status INTEGER NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    referrer_host TEXT,
    referrer TEXT,
    user_agent TEXT NOT NULL,
    ua_family TEXT NOT NULL,
    os_family TEXT NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0 CHECK (is_bot IN (0, 1)),
    is_asset INTEGER NOT NULL DEFAULT 0 CHECK (is_asset IN (0, 1)),
    country TEXT
);
CREATE INDEX IF NOT EXISTS requests_site_ts ON requests(site_id, ts);
CREATE INDEX IF NOT EXISTS requests_site_path ON requests(site_id, path);
CREATE INDEX IF NOT EXISTS requests_site_bot_ts ON requests(site_id, is_bot, ts);
CREATE INDEX IF NOT EXISTS requests_day ON requests(day);
CREATE INDEX IF NOT EXISTS requests_site_fingerprint
    ON requests(site_id, source_fingerprint);
CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    lines_seen INTEGER NOT NULL DEFAULT 0,
    parsed INTEGER NOT NULL DEFAULT 0,
    parse_failures INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS daily_site (
    site_id INTEGER NOT NULL REFERENCES sites(id), day TEXT NOT NULL,
    requests INTEGER NOT NULL, human_requests INTEGER NOT NULL,
    bot_requests INTEGER NOT NULL, unique_visitors INTEGER NOT NULL,
    bytes INTEGER NOT NULL, status_2xx INTEGER NOT NULL,
    status_3xx INTEGER NOT NULL, status_4xx INTEGER NOT NULL,
    status_5xx INTEGER NOT NULL, PRIMARY KEY(site_id, day)
);
CREATE TABLE IF NOT EXISTS daily_traffic (
    site_id INTEGER NOT NULL REFERENCES sites(id), day TEXT NOT NULL,
    is_bot INTEGER NOT NULL, is_asset INTEGER NOT NULL,
    requests INTEGER NOT NULL, unique_visitors INTEGER NOT NULL,
    bytes INTEGER NOT NULL, status_2xx INTEGER NOT NULL,
    status_3xx INTEGER NOT NULL, status_4xx INTEGER NOT NULL,
    status_5xx INTEGER NOT NULL,
    PRIMARY KEY(site_id, day, is_bot, is_asset)
);
CREATE TABLE IF NOT EXISTS daily_filter (
    site_id INTEGER NOT NULL REFERENCES sites(id), day TEXT NOT NULL,
    include_bots INTEGER NOT NULL, include_assets INTEGER NOT NULL,
    requests INTEGER NOT NULL, unique_visitors INTEGER NOT NULL,
    bytes INTEGER NOT NULL, status_2xx INTEGER NOT NULL,
    status_3xx INTEGER NOT NULL, status_4xx INTEGER NOT NULL,
    status_5xx INTEGER NOT NULL,
    PRIMARY KEY(site_id, day, include_bots, include_assets)
);
CREATE TABLE IF NOT EXISTS daily_path (
    site_id INTEGER NOT NULL REFERENCES sites(id), day TEXT NOT NULL,
    path TEXT NOT NULL, requests INTEGER NOT NULL,
    unique_visitors INTEGER NOT NULL, human_requests INTEGER NOT NULL,
    human_unique_visitors INTEGER NOT NULL, asset_requests INTEGER NOT NULL,
    PRIMARY KEY(site_id, day, path)
);
CREATE TABLE IF NOT EXISTS daily_referrer (
    site_id INTEGER NOT NULL REFERENCES sites(id), day TEXT NOT NULL,
    referrer_host TEXT NOT NULL, requests INTEGER NOT NULL,
    human_requests INTEGER NOT NULL, nonasset_requests INTEGER NOT NULL,
    human_nonasset_requests INTEGER NOT NULL,
    PRIMARY KEY(site_id, day, referrer_host)
);
CREATE TABLE IF NOT EXISTS daily_agent (
    site_id INTEGER NOT NULL REFERENCES sites(id), day TEXT NOT NULL,
    ua_family TEXT NOT NULL, os_family TEXT NOT NULL,
    is_bot INTEGER NOT NULL, requests INTEGER NOT NULL,
    asset_requests INTEGER NOT NULL,
    PRIMARY KEY(site_id, day, ua_family, os_family, is_bot)
);
CREATE TABLE IF NOT EXISTS daily_country (
    site_id INTEGER NOT NULL REFERENCES sites(id), day TEXT NOT NULL,
    country TEXT NOT NULL, requests INTEGER NOT NULL,
    human_requests INTEGER NOT NULL, nonasset_requests INTEGER NOT NULL,
    human_nonasset_requests INTEGER NOT NULL,
    PRIMARY KEY(site_id, day, country)
);
CREATE TABLE IF NOT EXISTS daily_status (
    site_id INTEGER NOT NULL REFERENCES sites(id), day TEXT NOT NULL,
    status INTEGER NOT NULL, requests INTEGER NOT NULL,
    human_requests INTEGER NOT NULL, nonasset_requests INTEGER NOT NULL,
    human_nonasset_requests INTEGER NOT NULL,
    PRIMARY KEY(site_id, day, status)
);
CREATE TABLE IF NOT EXISTS daily_404 (
    site_id INTEGER NOT NULL REFERENCES sites(id), day TEXT NOT NULL,
    path TEXT NOT NULL, requests INTEGER NOT NULL,
    human_requests INTEGER NOT NULL, nonasset_requests INTEGER NOT NULL,
    human_nonasset_requests INTEGER NOT NULL,
    PRIMARY KEY(site_id, day, path)
);
"""


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(path)
    if not readonly:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=30)
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}
    if "source_fingerprint" not in columns:
        conn.execute(
            "ALTER TABLE requests ADD COLUMN source_fingerprint TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        """
        UPDATE requests SET source_fingerprint=substr(source_key, -16)
        WHERE source_fingerprint=''
        """
    )


MIGRATIONS = {1: _migrate_1_to_2}


def _execute_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


def initialize(conn: sqlite3.Connection, config: Config) -> None:
    try:
        # Gunicorn workers can initialize concurrently. An immediate transaction
        # serializes version checks and migrations before either worker proceeds.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            _execute_schema(conn)
            conn.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
            )
        else:
            version = row["version"]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema {version} is newer than app schema {SCHEMA_VERSION}"
                )
            while version < SCHEMA_VERSION:
                migration = MIGRATIONS.get(version)
                if migration is None:
                    raise RuntimeError(
                        f"No migration from database schema {version} to "
                        f"app schema {SCHEMA_VERSION}"
                    )
                migration(conn)
                version += 1
                conn.execute("UPDATE schema_version SET version=?", (version,))
            _execute_schema(conn)
        conn.executemany(
            "INSERT INTO sites(name) VALUES (?) ON CONFLICT(name) DO NOTHING",
            ((site.name,) for site in config.sites),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def site_id_map(conn: sqlite3.Connection) -> dict[str, int]:
    return {row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM sites")}


def insert_requests(conn: sqlite3.Connection, rows: Iterable[tuple]) -> int:
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO requests(
            source_key, source_fingerprint, site_id, ts, day, ip_hash, method,
            path, query, status, bytes, referrer_host, referrer, user_agent,
            ua_family, os_family, is_bot, is_asset, country
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return conn.total_changes - before
