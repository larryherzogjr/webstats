from pathlib import Path
import tempfile
import unittest

import bcrypt

from webstats.chronicle import scan
from webstats.config import Config, GeoIPConfig, ServerConfig, SiteConfig, StorageConfig
from webstats.db import connect


class ChronicleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.config = Config(
            server=ServerConfig(
                "127.0.0.1:5010", "x" * 32, "admin",
                bcrypt.hashpw(b"password", bcrypt.gensalt()).decode(), "UTC",
            ),
            storage=StorageConfig(root / "test.db", root / "state.json", 90),
            geoip=GeoIPConfig(False, root / "geo.mmdb"),
            ip_hash_salt_rotation="daily", log_format="combined",
            sites=(SiteConfig("example.com", (root / "access.log",)),),
            source_path=root / "config.toml",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def robots(text="User-agent: *\nAllow: /\n"):
        return lambda names: {
            name: {
                "site": name, "url": f"https://{name}/robots.txt",
                "status": "available", "text": text,
            } for name in names
        }

    @staticmethod
    def inventory(pages, status="available", errors=None, limited=False):
        def fetch(names, robots):
            return {
                name: {
                    "site": name, "status": status, "declared": [],
                    "documents": [f"https://{name}/sitemap.xml"] if status == "available" else [],
                    "pages": [
                        {
                            "path": path, "url": f"https://{name}{path}",
                            "lastmod": lastmod, "sitemap": f"https://{name}/sitemap.xml",
                        } for path, lastmod in pages
                    ],
                    "errors": list(errors or []), "limited": limited,
                } for name in names
            }
        return fetch

    def test_tracks_publish_update_redirect_policy_and_republish(self):
        first = scan(
            self.config, now=1_790_640_000, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory([
                ("/old", "2026-09-28"), ("/gone", "2026-09-28")
            ]),
        )
        self.assertEqual(first["events"], 1)

        second = scan(
            self.config, now=1_790_726_400,
            robots_fetcher=self.robots("User-agent: GPTBot\nDisallow: /private\n"),
            sitemap_fetcher=self.inventory([
                ("/old", "2026-09-29"), ("/new", "2026-09-29")
            ]),
            page_probe=lambda url, site: {
                "status": 301, "redirect_to": "https://example.com/new"
            },
        )
        self.assertEqual(second["probes"], 1)
        with connect(self.config.storage.db_path, readonly=True) as conn:
            kinds = {
                row[0] for row in conn.execute(
                    "SELECT kind FROM chronicle_events WHERE occurred_at=?",
                    (1_790_726_400,),
                )
            }
            redirected = conn.execute(
                "SELECT state, http_status, redirect_to FROM chronicle_pages "
                "WHERE path='/gone'"
            ).fetchone()
        self.assertEqual(
            kinds, {"robots_changed", "published", "updated", "redirected"}
        )
        self.assertEqual(tuple(redirected), (
            "redirected", 301, "https://example.com/new"
        ))

        scan(
            self.config, now=1_790_812_800,
            robots_fetcher=self.robots("User-agent: GPTBot\nDisallow: /private\n"),
            sitemap_fetcher=self.inventory([
                ("/old", "2026-09-29"), ("/new", "2026-09-29"),
                ("/gone", "2026-09-30"),
            ]),
        )
        with connect(self.config.storage.db_path, readonly=True) as conn:
            state = conn.execute(
                "SELECT state, removed_at FROM chronicle_pages WHERE path='/gone'"
            ).fetchone()
            republished = conn.execute(
                "SELECT COUNT(*) FROM chronicle_events WHERE kind='republished'"
            ).fetchone()[0]
        self.assertEqual(tuple(state), ("published", None))
        self.assertEqual(republished, 1)

    def test_unavailable_inventory_never_removes_known_pages(self):
        scan(
            self.config, now=1_790_640_000, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory([("/safe", None)]),
        )
        scan(
            self.config, now=1_790_726_400, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory([], status="unavailable"),
            page_probe=lambda url, site: self.fail("unavailable inventory was probed"),
        )
        with connect(self.config.storage.db_path, readonly=True) as conn:
            state = conn.execute(
                "SELECT state FROM chronicle_pages WHERE path='/safe'"
            ).fetchone()[0]
        self.assertEqual(state, "published")

    def test_partial_inventory_never_removes_or_probes_known_pages(self):
        scan(
            self.config, now=1_790_640_000, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory([("/safe", None), ("/other", None)]),
        )
        scan(
            self.config, now=1_790_726_400, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory(
                [("/safe", None)],
                errors=[{"url": "https://example.com/child.xml", "status": "unavailable"}],
            ),
            page_probe=lambda url, site: self.fail("partial inventory was probed"),
        )
        with connect(self.config.storage.db_path, readonly=True) as conn:
            state = conn.execute(
                "SELECT state FROM chronicle_pages WHERE path='/other'"
            ).fetchone()[0]
        self.assertEqual(state, "published")

    def test_probe_network_failure_preserves_page_state(self):
        scan(
            self.config, now=1_790_640_000, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory([("/safe", None)]),
        )
        scan(
            self.config, now=1_790_726_400, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory([]),
            page_probe=lambda url, site: {"status": None, "redirect_to": None},
        )
        with connect(self.config.storage.db_path, readonly=True) as conn:
            state = conn.execute(
                "SELECT state, removed_at FROM chronicle_pages WHERE path='/safe'"
            ).fetchone()
            events = conn.execute(
                "SELECT COUNT(*) FROM chronicle_events WHERE path='/safe'"
            ).fetchone()[0]
        self.assertEqual(tuple(state), ("published", None))
        self.assertEqual(events, 0)

    def test_robots_outage_is_not_a_policy_change(self):
        scan(
            self.config, now=1_790_640_000, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory([]),
        )

        def unavailable(names):
            return {
                name: {
                    "site": name, "url": f"https://{name}/robots.txt",
                    "status": "unavailable", "text": "",
                }
                for name in names
            }

        scan(
            self.config, now=1_790_726_400, robots_fetcher=unavailable,
            sitemap_fetcher=self.inventory([]),
        )
        with connect(self.config.storage.db_path, readonly=True) as conn:
            changes = conn.execute(
                "SELECT COUNT(*) FROM chronicle_events WHERE kind='robots_changed'"
            ).fetchone()[0]
        self.assertEqual(changes, 0)

    def test_removal_probe_does_not_hold_writer_lock(self):
        scan(
            self.config, now=1_790_640_000, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory([("/safe", None)]),
        )

        def probe(_url, _site):
            with connect(self.config.storage.db_path) as other:
                other.execute(
                    "INSERT INTO ingest_runs(started_at, status) VALUES (1, 'ok')"
                )
                other.commit()
            return {"status": 200, "redirect_to": None}

        scan(
            self.config, now=1_790_726_400, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory([]), page_probe=probe,
        )

    def test_unlisted_page_is_monitored_for_later_disappearance(self):
        scan(
            self.config, now=1_790_640_000, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory([("/later", None)]),
        )
        scan(
            self.config, now=1_790_726_400, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory([]),
            page_probe=lambda url, site: {"status": 200, "redirect_to": None},
        )
        scan(
            self.config, now=1_790_812_800, robots_fetcher=self.robots(),
            sitemap_fetcher=self.inventory([]),
            page_probe=lambda url, site: {"status": 410, "redirect_to": None},
        )
        with connect(self.config.storage.db_path, readonly=True) as conn:
            state = conn.execute(
                "SELECT state, http_status FROM chronicle_pages WHERE path='/later'"
            ).fetchone()
            kinds = [
                row[0] for row in conn.execute(
                    "SELECT kind FROM chronicle_events WHERE path='/later' "
                    "ORDER BY occurred_at"
                )
            ]
        self.assertEqual(tuple(state), ("disappeared", 410))
        self.assertEqual(kinds, ["sitemap_removed", "disappeared"])


if __name__ == "__main__":
    unittest.main()
