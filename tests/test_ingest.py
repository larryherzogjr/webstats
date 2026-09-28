import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from webstats.config import (
    Config, GeoIPConfig, ServerConfig, SiteConfig, StorageConfig,
)
from webstats.ingest import ingest_once


def log_line(path="/one", status=200):
    return (
        f'203.0.113.10 - - [28/Sep/2026:10:15:00 -0500] "GET {path} HTTP/1.1" '
        f'{status} 42 "-" "Mozilla/5.0 AppleWebKit/537.36 Chrome/128.0 Safari/537.36"\n'
    )


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.log = self.root / "example.access.log"
        self.config = Config(
            server=ServerConfig("127.0.0.1:5010", "secret", "admin", "hash", "UTC"),
            storage=StorageConfig(self.root / "test.db", self.root / "state.json", 90),
            geoip=GeoIPConfig(False, self.root / "geo.mmdb"),
            ip_hash_salt_rotation="daily",
            log_format="combined",
            sites=(SiteConfig("example.com", (self.log,)),),
            source_path=self.root / "config.toml",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def count(self):
        with sqlite3.connect(self.config.storage.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

    def test_partial_line_waits_for_newline(self):
        self.log.write_text(log_line().rstrip("\n"))
        stats = ingest_once(self.config)
        self.assertEqual(stats.inserted, 0)
        self.assertEqual(self.count(), 0)
        with self.log.open("a") as handle:
            handle.write("\n")
        stats = ingest_once(self.config)
        self.assertEqual(stats.inserted, 1)
        self.assertEqual(self.count(), 1)

    def test_duplicate_run_does_not_double_count(self):
        self.log.write_text(log_line())
        ingest_once(self.config)
        self.config.storage.state_path.unlink()
        stats = ingest_once(self.config)
        self.assertEqual(stats.inserted, 0)
        self.assertEqual(self.count(), 1)

    def test_rotation_finishes_old_file_then_reads_new(self):
        self.log.write_text(log_line("/one"))
        ingest_once(self.config)
        with self.log.open("a") as handle:
            handle.write(log_line("/two"))
        rotated = Path(str(self.log) + ".1")
        self.log.rename(rotated)
        self.log.write_text(log_line("/three"))
        stats = ingest_once(self.config)
        self.assertEqual(stats.inserted, 2)
        self.assertEqual(self.count(), 3)
        with sqlite3.connect(self.config.storage.db_path) as conn:
            paths = [row[0] for row in conn.execute("SELECT path FROM requests ORDER BY id")]
        self.assertEqual(paths, ["/one", "/two", "/three"])

    def test_shrunk_file_restarts_safely(self):
        self.log.write_text(log_line("/longer-path"))
        ingest_once(self.config)
        self.log.write_text(log_line("/x"))
        stats = ingest_once(self.config)
        self.assertEqual(stats.inserted, 1)
        self.assertEqual(self.count(), 2)


if __name__ == "__main__":
    unittest.main()
