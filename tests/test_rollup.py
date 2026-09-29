from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from webstats.config import Config, GeoIPConfig, ServerConfig, SiteConfig, StorageConfig
from webstats.db import connect, initialize, insert_requests, site_id_map
from webstats.rollup import (
    _refresh_summary_moments,
    maintain_rollups,
    recompute_day,
)


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

    def test_performance_rollup_separates_application_health_from_probes(self):
        site_id = site_id_map(self.conn)["example.com"]
        observations = (
            ("/essay", 200, 1000, 100),
            ("/essay", 200, 3000, 300),
            ("/essay", 200, 5000, 900),
            ("/missing", 404, 100, 20),
            ("/broken", 500, 200, 80),
            ("/.env", 404, 50, 10),
        )
        rows = []
        for index, (path, status, size, timing) in enumerate(observations):
            rows.append((
                f"performance-{index}", f"performance-fingerprint-{index}",
                site_id, index + 1, "2026-09-28", f"visitor-{index}", "GET",
                path, None, status, size, None, None, "Mozilla/5.0",
                "Other browser", "Other", 0, 0, None, timing,
            ))
        insert_requests(self.conn, rows)
        recompute_day(self.conn, "2026-09-28")

        page = self.conn.execute(
            "SELECT requests, timed_requests, p50_ms, p95_ms, max_ms, "
            "avg_bytes, max_bytes FROM daily_performance WHERE path='/essay'"
        ).fetchone()
        self.assertEqual(tuple(page), (3, 3, 300, 900, 900, 3000, 5000))
        site = self.conn.execute(
            "SELECT app_4xx, app_5xx, scanner_requests, scanner_errors "
            "FROM daily_reliability"
        ).fetchone()
        self.assertEqual(tuple(site), (1, 1, 1, 1))

    def test_journeys_collapse_refreshes_split_sessions_and_ignore_noise(self):
        site_id = site_id_map(self.conn)["example.com"]
        rows = []
        observations = (
            ("a", 100, "/start", 200, 0, 0),
            ("a", 110, "/start", 200, 0, 0),
            ("a", 120, "/middle", 200, 0, 0),
            ("a", 130, "/end", 200, 0, 0),
            ("a", 2000, "/later", 200, 0, 0),
            ("b", 140, "/start", 200, 0, 0),
            ("c", 150, "/bot", 200, 1, 0),
            ("d", 160, "/asset.css", 200, 0, 1),
            ("e", 170, "/missing", 404, 0, 0),
        )
        for index, (visitor, ts, path, status, bot, asset) in enumerate(observations):
            rows.append((
                f"journey-source-{index}", f"journey-fingerprint-{index}",
                site_id, ts, "2026-09-28", visitor, "GET", path, None,
                status, 10, None, None, "Mozilla/5.0", "Other browser",
                "Other", bot, asset, None,
            ))
        insert_requests(self.conn, rows)
        recompute_day(self.conn, "2026-09-28")

        summary = self.conn.execute(
            "SELECT sessions, pageviews, path_steps, single_page_sessions, "
            "multi_page_sessions, max_depth, duration_seconds "
            "FROM daily_journey WHERE site_id=? AND day='2026-09-28'",
            (site_id,),
        ).fetchone()
        self.assertEqual(tuple(summary), (3, 6, 5, 2, 1, 3, 30))
        transitions = {
            (row["from_path"], row["to_path"], row["transitions"])
            for row in self.conn.execute(
                "SELECT from_path, to_path, transitions "
                "FROM daily_journey_transition"
            )
        }
        self.assertEqual(
            transitions,
            {("/start", "/middle", 1), ("/middle", "/end", 1)},
        )
        start = self.conn.execute(
            "SELECT entrances, exits, single_page_sessions "
            "FROM daily_journey_endpoint WHERE path='/start'"
        ).fetchone()
        self.assertEqual(tuple(start), (2, 1, 1))

    def test_privacy_protected_site_has_no_journey_rollup(self):
        self.conn.execute("INSERT INTO sites(name) VALUES ('ad-fontes.app')")
        private_id = site_id_map(self.conn)["ad-fontes.app"]
        rows = [
            (
                f"private-source-{index}", f"private-fingerprint-{index}",
                private_id, 100 + index, "2026-09-28", "private-reader",
                "GET", path, None, 200, 10, None, None, "Mozilla/5.0",
                "Other browser", "Other", 0, 0, None,
            )
            for index, path in enumerate(("/private-one", "/private-two"))
        ]
        insert_requests(self.conn, rows)
        recompute_day(self.conn, "2026-09-28")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM daily_journey WHERE site_id=?",
                (private_id,),
            ).fetchone()[0],
            0,
        )

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
            self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 4
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

        pages = {
            (row["day"], row["path"])
            for row in self.conn.execute(
                "SELECT day, path FROM events WHERE kind='new_page'"
            )
        }
        self.assertEqual(
            pages,
            {("2026-09-27", "/older-essay"), ("2026-09-28", "/essay")},
        )

    def test_new_page_moments_ignore_probes_and_keep_first_sighting(self):
        site_id = site_id_map(self.conn)["example.com"]
        rows = [
            (
                "source-page", "fingerprint-page", site_id, 1790600000,
                "2026-09-28", "visitor", "GET", "/new-essay", None, 200,
                10, None, None, "Mozilla/5.0", "Other browser", "Other",
                0, 0, "US",
            ),
            (
                "source-probe", "fingerprint-probe", site_id, 1790600001,
                "2026-09-28", "visitor-2", "GET", "/wp-login.php", None,
                200, 10, None, None, "Mozilla/5.0", "Other browser", "Other",
                0, 0, "US",
            ),
            (
                "source-failed", "fingerprint-failed", site_id, 1790600002,
                "2026-09-28", "visitor-3", "GET", "/missing", None, 404,
                10, None, None, "Mozilla/5.0", "Other browser", "Other",
                0, 0, "US",
            ),
        ]
        insert_requests(self.conn, rows)
        recompute_day(self.conn, "2026-09-28")
        later = (
            "source-page-later", "fingerprint-page-later", site_id, 1790686400,
            "2026-09-29", "visitor-4", "GET", "/new-essay", None, 200,
            10, None, None, "Mozilla/5.0", "Other browser", "Other",
            0, 0, "CA",
        )
        insert_requests(self.conn, [later])
        recompute_day(self.conn, "2026-09-29")

        pages = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT day, path, country FROM events WHERE kind='new_page'"
            )
        ]
        self.assertEqual(pages, [("2026-09-28", "/new-essay", "US")])

    def test_resurfaced_content_moment_after_thirty_quiet_days(self):
        site_id = site_id_map(self.conn)["example.com"]
        old = (
            "old-revival", "old-revival-fingerprint", site_id, 1785592800,
            "2026-08-01", "old-reader", "GET", "/evergreen", None, 200,
            10, None, None, "Mozilla/5.0", "Other browser", "Other",
            0, 0, "US",
        )
        current = (
            "new-revival", "new-revival-fingerprint", site_id, 1790600000,
            "2026-09-28", "new-reader", "GET", "/evergreen", None, 200,
            10, None, None, "Mozilla/5.0", "Other browser", "Other",
            0, 0, "CA",
        )
        insert_requests(self.conn, [old])
        recompute_day(self.conn, "2026-08-01")
        insert_requests(self.conn, [current])
        recompute_day(self.conn, "2026-09-28")
        recompute_day(self.conn, "2026-09-28")

        rows = [
            tuple(row) for row in self.conn.execute(
                """
                SELECT day, path, source FROM events
                WHERE kind='content_resurfaced'
                """
            )
        ]
        self.assertEqual(rows, [("2026-09-28", "/evergreen", "2026-08-01")])

    def test_summary_moments_are_meaningful_and_idempotent(self):
        site_id = site_id_map(self.conn)["example.com"]
        start = date(2026, 9, 1)
        rows = []
        for offset in range(17):
            day = (start + timedelta(days=offset)).isoformat()
            requests = 50 if offset == 0 else 2
            visitors = 90 if offset == 0 else 15 if offset == 1 else 1
            if offset == 15:
                requests = 20
            if offset == 16:
                requests = 60
            rows.append(
                (
                    site_id, day, 0, 0, requests, visitors, requests * 10,
                    requests, 0, 0, 0,
                )
            )
        self.conn.executemany(
            """
            INSERT INTO daily_filter(
                site_id, day, include_bots, include_assets, requests,
                unique_visitors, bytes, status_2xx, status_3xx,
                status_4xx, status_5xx
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.executemany(
            """
            INSERT INTO daily_feed_reader(
                site_id, day, path, reader, requests, reported_subscribers,
                first_seen, last_seen
            ) VALUES (?, ?, '/feed.xml', 'Inoreader', 1, ?, ?, ?)
            """,
            (
                (site_id, "2026-09-01", 42, 1790600000, 1790600000),
                (site_id, "2026-09-02", 55, 1790686400, 1790686400),
            ),
        )
        _refresh_summary_moments(self.conn, "UTC")
        _refresh_summary_moments(self.conn, "UTC")

        moments = [
            tuple(row)
            for row in self.conn.execute(
                """
                SELECT kind, day, value, source
                FROM events ORDER BY day, kind
                """
            )
        ]
        self.assertEqual(
            moments,
            [
                ("feed_subscriber_milestone", "2026-09-01", 25, "Inoreader"),
                ("feed_subscriber_milestone", "2026-09-02", 50, "Inoreader"),
                ("visitor_milestone", "2026-09-02", 100, None),
                ("traffic_spike", "2026-09-16", 20, None),
                ("traffic_record", "2026-09-17", 60, None),
            ],
        )

    def test_feed_reader_rollup_keeps_reported_counts_and_observations(self):
        site_id = site_id_map(self.conn)["example.com"]
        rows = [
            (
                "source-inoreader", "fingerprint-inoreader", site_id, 1790600000,
                "2026-09-28", "reader", "GET", "/feed.xml", None, 200, 10,
                None, None,
                "Inoreader/1.0 (+http://www.inoreader.com/feed-fetcher; 7 subscribers; )",
                "Inoreader", "Other", 1, 0, "US",
            ),
            (
                "source-feedly", "fingerprint-feedly", site_id, 1790600001,
                "2026-09-28", "reader-2", "GET", "/feed.xml", None, 200, 10,
                None, None, "Feedly/1.0", "Feedly", "Other", 1, 0, "US",
            ),
        ]
        insert_requests(self.conn, rows)
        recompute_day(self.conn, "2026-09-28")
        recompute_day(self.conn, "2026-09-28")

        observed = {
            row["reader"]: (row["requests"], row["reported_subscribers"])
            for row in self.conn.execute(
                "SELECT reader, requests, reported_subscribers FROM daily_feed_reader"
            )
        }
        self.assertEqual(observed, {"Inoreader": (1, 7), "Feedly": (1, None)})


if __name__ == "__main__":
    unittest.main()
