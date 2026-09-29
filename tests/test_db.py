from pathlib import Path
import sqlite3
import tempfile
import unittest

from webstats.config import Config, GeoIPConfig, ServerConfig, SiteConfig, StorageConfig
from webstats.db import SCHEMA_VERSION, connect, initialize


class DatabaseMigrationTests(unittest.TestCase):
    def config(self, root: Path) -> Config:
        return Config(
            server=ServerConfig("127.0.0.1:5010", "secret", "admin", "hash", "UTC"),
            storage=StorageConfig(root / "test.db", root / "state.json", 90),
            geoip=GeoIPConfig(False, root / "geo.mmdb"),
            ip_hash_salt_rotation="daily",
            log_format="combined",
            sites=(SiteConfig("example.com", (root / "access.log",)),),
            source_path=root / "config.toml",
        )

    def test_new_database_uses_latest_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with connect(root / "test.db") as conn:
                initialize(conn, self.config(root))
                version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
                columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(requests)")
                }
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual(version, SCHEMA_VERSION)
            self.assertIn("source_fingerprint", columns)
            self.assertTrue(
                {
                    "daily_page_referrer", "daily_page_agent",
                    "daily_page_country", "daily_page_status", "events",
                } <= tables
            )

    def test_version_one_database_is_migrated_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = sqlite3.connect(root / "test.db")
            raw.executescript(
                """
                CREATE TABLE schema_version(version INTEGER NOT NULL);
                INSERT INTO schema_version VALUES (1);
                CREATE TABLE sites(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE
                );
                INSERT INTO sites(id, name) VALUES (1, 'example.com');
                CREATE TABLE requests(
                    id INTEGER PRIMARY KEY,
                    source_key TEXT NOT NULL UNIQUE,
                    site_id INTEGER NOT NULL,
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
                    is_bot INTEGER NOT NULL,
                    is_asset INTEGER NOT NULL,
                    country TEXT
                );
                INSERT INTO requests(
                    source_key, site_id, ts, day, ip_hash, method, path,
                    status, bytes, user_agent, ua_family, os_family,
                    is_bot, is_asset
                ) VALUES (
                    '/log:123:0:0123456789abcdef', 1, 1, '2026-09-28',
                    'visitor', 'GET', '/', 200, 10, 'GPTBot/1.2',
                    'GPTBot', 'Other', 1, 0
                );
                """
            )
            raw.close()

            with connect(root / "test.db") as conn:
                initialize(conn, self.config(root))
                version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
                fingerprint = conn.execute(
                    "SELECT source_fingerprint FROM requests"
                ).fetchone()[0]
                page_agent = conn.execute(
                    "SELECT path, ua_family FROM daily_page_agent"
                ).fetchone()
                event = conn.execute(
                    "SELECT kind, agent FROM events"
                ).fetchone()
                page_status = conn.execute(
                    "SELECT path, status, requests FROM daily_page_status"
                ).fetchone()
            self.assertEqual(version, SCHEMA_VERSION)
            self.assertEqual(fingerprint, "0123456789abcdef")
            self.assertEqual(tuple(page_agent), ("/", "GPTBot"))
            self.assertEqual(tuple(event), ("first_ai_visit", "GPTBot"))
            self.assertEqual(tuple(page_status), ("/", 200, 1))

    def test_newer_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with connect(root / "test.db") as conn:
                conn.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
                conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION + 1,))
                conn.commit()
                with self.assertRaises(RuntimeError):
                    initialize(conn, self.config(root))


if __name__ == "__main__":
    unittest.main()
