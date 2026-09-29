from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from webstats.config import Config, GeoIPConfig, ServerConfig, SiteConfig, StorageConfig
from webstats.db import connect, initialize, insert_requests, site_id_map
from webstats.rollup import maintain_rollups, recompute_day


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.config = Config(
            server=ServerConfig("127.0.0.1:5010", "secret", "admin", "hash", "UTC"),
            storage=StorageConfig(root / "test.db", root / "state.json", 90),
            geoip=GeoIPConfig(False, root / "geo.mmdb"),
            ip_hash_salt_rotation="daily",
            log_format="combined",
            sites=(SiteConfig("example.com", (root / "access.log",)),),
            source_path=root / "config.toml",
        )
        self.conn = connect(self.config.storage.db_path)
        initialize(self.conn, self.config)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def test_recompute_is_idempotent(self):
        site_id = site_id_map(self.conn)["example.com"]
        row = (
            "source-1", "fingerprint-1", site_id, 1790600000, "2026-09-28", "visitor", "GET",
            "/", None, 200, 10, None, None, "Mozilla/5.0", "Other browser",
            "Other", 0, 0, None,
        )
        insert_requests(self.conn, [row])
        recompute_day(self.conn, "2026-09-28")
        first = tuple(self.conn.execute("SELECT * FROM daily_site").fetchone())
        recompute_day(self.conn, "2026-09-28")
        second = tuple(self.conn.execute("SELECT * FROM daily_site").fetchone())
        self.assertEqual(first, second)

    def test_retention_keeps_rollup(self):
        old_day = (date.today() - timedelta(days=200)).isoformat()
        site_id = site_id_map(self.conn)["example.com"]
        row = (
            "source-old", "fingerprint-old", site_id, 1, old_day, "visitor", "GET", "/old", None,
            200, 10, None, None, "Mozilla/5.0", "Other browser", "Other", 0, 0, None,
        )
        insert_requests(self.conn, [row])
        maintain_rollups(self.conn, 90)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT requests FROM daily_site WHERE day=?", (old_day,)).fetchone()[0], 1)

    def test_events_and_page_dimensions_are_idempotent(self):
        site_id = site_id_map(self.conn)["example.com"]
        rows = [
            (
                "source-referrer", "fingerprint-referrer", site_id, 1790600000,
                "2026-09-28", "visitor", "GET", "/essay", None, 200, 10,
                "news.ycombinator.com", "https://news.ycombinator.com/item?id=1",
                "Mozilla/5.0", "Other browser", "Other", 0, 0, "US",
            ),
            (
                "source-ai", "fingerprint-ai", site_id, 1790600001,
                "2026-09-28", "crawler", "GET", "/essay", None, 200, 10,
                None, None, "GPTBot/1.2", "GPTBot", "Other", 1, 0, "US",
            ),
        ]
        insert_requests(self.conn, rows)
        recompute_day(self.conn, "2026-09-28")
        recompute_day(self.conn, "2026-09-28")

        older = (
            "source-referrer-older", "fingerprint-referrer-older", site_id,
            1790500000, "2026-09-27", "older-visitor", "GET", "/older-essay",
            None, 200, 10, "news.ycombinator.com",
            "https://news.ycombinator.com/item?id=0", "Mozilla/5.0",
            "Other browser", "Other", 0, 0, "CA",
        )
        insert_requests(self.conn, [older])
        recompute_day(self.conn, "2026-09-27")

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2
        )
        first_referrer = self.conn.execute(
            "SELECT day, path, country FROM events WHERE kind='new_referrer'"
        ).fetchone()
        self.assertEqual(tuple(first_referrer), ("2026-09-27", "/older-essay", "CA"))
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT path, referrer_host, human_nonasset_requests "
                    "FROM daily_page_referrer WHERE day='2026-09-28'"
                ).fetchone()
            ),
            ("/essay", "news.ycombinator.com", 1),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT path, ua_family, requests FROM daily_page_agent "
                    "WHERE ua_family='GPTBot'"
                ).fetchone()
            ),
            ("/essay", "GPTBot", 1),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT path, country, requests, human_nonasset_requests "
                    "FROM daily_page_country WHERE day='2026-09-28' AND country='US'"
                ).fetchone()
            ),
            ("/essay", "US", 2, 1),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT path, status, requests, human_nonasset_requests "
                    "FROM daily_page_status WHERE day='2026-09-28' AND status=200"
                ).fetchone()
            ),
            ("/essay", 200, 2, 1),
        )


if __name__ == "__main__":
    unittest.main()
