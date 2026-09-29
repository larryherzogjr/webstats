from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import bcrypt

from webstats.app import create_app
from webstats.api import _referrer_group
from webstats.auth import _attempts, _credential_token
from webstats.config import Config, GeoIPConfig, ServerConfig, SiteConfig, StorageConfig
from webstats.db import connect, insert_requests, site_id_map
from webstats.ingest import ingest_once
from webstats.rollup import recompute_day


def line(
    ip,
    path,
    status=200,
    ua="Mozilla/5.0 AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
    referrer="-",
):
    return (
        f'{ip} - - [28/Sep/2026:10:15:00 -0500] "GET {path} HTTP/1.1" '
        f'{status} 42 "{referrer}" "{ua}"\n'
    )


class ApiTests(unittest.TestCase):
    def setUp(self):
        _attempts.clear()
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.log = root / "example.access.log"
        self.log.write_text(
            line("203.0.113.1", "/one")
            + line("203.0.113.1", "/app.css")
            + line("203.0.113.2", "/bot-missing", 404, "Googlebot/2.1")
            + line("203.0.113.3", "/missing", 404)
        )
        password_hash = bcrypt.hashpw(b"password", bcrypt.gensalt()).decode()
        self.config = Config(
            server=ServerConfig("127.0.0.1:5010", "secret", "admin", password_hash, "America/Chicago"),
            storage=StorageConfig(root / "test.db", root / "state.json", 90),
            geoip=GeoIPConfig(False, root / "geo.mmdb"),
            ip_hash_salt_rotation="daily",
            log_format="combined",
            sites=(SiteConfig("example.com", (self.log,)),),
            source_path=root / "config.toml",
        )
        ingest_once(self.config)
        self.app = create_app(self.config)
        self.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = self.app.test_client()

    def tearDown(self):
        _attempts.clear()
        self.tempdir.cleanup()

    def authenticate(self):
        with self.client.session_transaction() as session:
            session.permanent = True
            session["authenticated"] = True
            session["credential_token"] = _credential_token(self.config)

    def test_api_requires_authentication(self):
        response = self.client.get("/api/sites")
        self.assertEqual(response.status_code, 401)

    def test_login_and_html_pages(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/login").status_code, 200)
        response = self.client.post(
            "/login", data={"username": "admin", "password": "password"}
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertTrue(session.permanent)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/site/example.com").status_code, 200)
        page = self.client.get("/site/example.com/page?path=/one")
        self.assertEqual(page.status_code, 200)
        self.assertIn("/one", page.get_data(as_text=True))
        self.assertEqual(self.client.get("/site/example.com/page").status_code, 400)
        self.assertEqual(self.client.get("/almanac").status_code, 200)
        self.assertEqual(self.client.get("/briefings").status_code, 200)
        self.assertEqual(self.client.get("/changes").status_code, 200)
        self.assertEqual(self.client.get("/content").status_code, 200)
        self.assertEqual(self.client.get("/chronicle").status_code, 200)
        self.assertEqual(self.client.get("/episodes").status_code, 200)
        self.assertEqual(self.client.get("/reliability").status_code, 200)
        self.assertEqual(self.client.get("/pulse").status_code, 200)
        self.assertEqual(self.client.get("/errors").status_code, 200)
        self.assertEqual(self.client.get("/journeys").status_code, 200)
        self.assertEqual(self.client.get("/links").status_code, 200)
        self.assertEqual(self.client.get("/inbox").status_code, 200)
        self.assertEqual(self.client.get("/galaxy").status_code, 200)
        self.assertEqual(self.client.get("/feed-readers").status_code, 200)
        self.assertEqual(self.client.get("/ai-crawlers").status_code, 200)
        self.assertEqual(self.client.get("/ai-policy").status_code, 200)
        self.assertEqual(self.client.get("/live").status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/site/not-configured.test").status_code, 404)
        reliability = self.client.get("/reliability").get_data(as_text=True)
        self.assertIn('class="nav-group active"', reliability)
        self.assertIn('href="/reliability" aria-current="page"', reliability)
        overview = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="nav-link active" href="/" aria-current="page"', overview)

    def test_login_throttle_uses_forwarded_client_address(self):
        first = {"X-Forwarded-For": "198.51.100.10"}
        second = {"X-Forwarded-For": "198.51.100.11"}
        with patch("webstats.auth.bcrypt.checkpw", return_value=False):
            for _ in range(8):
                response = self.client.post(
                    "/login",
                    data={"username": "admin", "password": "wrong"},
                    headers=first,
                )
                self.assertEqual(response.status_code, 200)
            self.assertEqual(
                self.client.post(
                    "/login",
                    data={"username": "admin", "password": "wrong"},
                    headers=first,
                ).status_code,
                429,
            )
            self.assertEqual(
                self.client.post(
                    "/login",
                    data={"username": "admin", "password": "wrong"},
                    headers=second,
                ).status_code,
                200,
            )

    def test_geoip_attribution_appears_when_enabled(self):
        config = replace(
            self.config,
            geoip=GeoIPConfig(True, self.config.geoip.db_path),
        )
        app = create_app(config)
        app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        client = app.test_client()
        with client.session_transaction() as session:
            session.permanent = True
            session["authenticated"] = True
            session["credential_token"] = _credential_token(config)
        response = client.get("/site/example.com")
        self.assertIn(
            '<a href="https://db-ip.com"', response.get_data(as_text=True)
        )

    def test_credential_change_invalidates_existing_session(self):
        self.authenticate()
        self.assertEqual(self.client.get("/").status_code, 200)
        changed = replace(
            self.config,
            server=replace(self.config.server, admin_user="new-admin"),
        )
        self.app.config["WEBSTATS_CONFIG"] = changed
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_nonpermanent_legacy_session_is_invalidated(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["credential_token"] = _credential_token(self.config)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_health_is_public_and_safe(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["logs"][0]["id"], "log-1")
        self.assertNotIn(str(self.log), response.get_data(as_text=True))
        self.assertIsNone(body["last_chronicle"])
        self.assertIsNotNone(body["rollups"]["latest_day"])
        self.assertGreaterEqual(body["rollups"]["open_days"], 1)
        self.assertEqual(body["rollups"]["sealed_days"], 0)
        self.assertEqual(body["rollups"]["late_requests"], 0)

    def test_overview_defaults_to_humans_without_assets(self):
        self.authenticate()
        response = self.client.get("/api/overview?from=2026-09-28&to=2026-09-28")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["sites"][0]["requests"], 2)
        self.assertEqual(body["totals"]["client_error_rate"], 50.0)
        self.assertEqual(body["totals"]["server_error_rate"], 0.0)
        self.assertEqual(body["totals"]["error_rate"], 50.0)
        response = self.client.get("/api/overview?from=2026-09-28&to=2026-09-28&bots=1&assets=1")
        self.assertEqual(response.get_json()["sites"][0]["requests"], 4)

    def test_chronicle_starts_empty(self):
        self.authenticate()
        response = self.client.get(
            "/api/chronicle?from=2026-09-28&to=2026-09-29"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["events"], [])
        self.assertEqual(body["latest"], [])

    def test_chronicle_search_and_crawler_reactions(self):
        self.authenticate()
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            snapshot = conn.execute(
                """
                INSERT INTO chronicle_snapshots(
                    site_id, captured_at, day, sitemap_status, sitemap_digest,
                    page_count, robots_status, robots_digest, robots_text
                ) VALUES (?, 1790630000, '2026-09-28', 'available', 'map', 1,
                          'available', 'rules', 'User-agent: *')
                """,
                (site_id,),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO chronicle_events(
                    site_id, snapshot_id, occurred_at, day, kind, path,
                    summary, event_key
                ) VALUES (?, ?, 1790630000, '2026-09-28', 'published',
                          '/essay', 'A page appeared.', 'chronicle-test')
                """,
                (site_id, snapshot),
            )
            conn.execute(
                """
                INSERT INTO daily_page_agent(
                    site_id, day, path, ua_family, is_bot, requests,
                    asset_requests
                ) VALUES (?, '2026-09-29', '/essay', 'GPTBot', 1, 3, 0)
                """,
                (site_id,),
            )
            conn.commit()
        response = self.client.get(
            "/api/chronicle?from=2026-09-28&to=2026-09-29&q=essay&kind=published"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["events"][0]["path"], "/essay")
        self.assertEqual(body["events"][0]["crawler_reaction"]["ai_requests"], 3)
        self.assertEqual(body["policy_versions"][0]["robots_digest"], "rules")

    def test_overview_includes_zero_traffic_days(self):
        self.authenticate()
        response = self.client.get(
            "/api/overview?from=2026-09-27&to=2026-09-29"
        )
        series = response.get_json()["timeseries"]
        self.assertEqual(
            [(row["day"], row["requests"]) for row in series],
            [("2026-09-27", 0), ("2026-09-28", 2), ("2026-09-29", 0)],
        )

    def test_live_radar_includes_ad_fontes_activity(self):
        with connect(self.config.storage.db_path) as conn:
            row = conn.execute(
                "SELECT ts, day FROM requests WHERE path='/one'"
            ).fetchone()
            conn.execute("UPDATE requests SET country='US' WHERE path='/one'")
            example_id = site_id_map(conn)["example.com"]
            conn.execute(
                """
                INSERT INTO events(
                    site_id, kind, event_key, occurred_at, day, value
                ) VALUES (?, 'traffic_record', 'live-record', ?, ?, 12)
                """,
                (example_id, row["ts"], row["day"]),
            )
            private_id = conn.execute(
                "INSERT INTO sites(name) VALUES ('ad-fontes.app')"
            ).lastrowid
            insert_requests(
                conn,
                [(
                    "private-live", "private-live-fingerprint", private_id,
                    row["ts"] + 1, row["day"], "private-visitor", "GET",
                    "/reader/private", None, 200, 42, None, None,
                    "Mozilla/5.0 AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
                    "Chrome", "Other", 0, 0, "GB",
                )],
            )
            conn.commit()
            now = datetime.fromtimestamp(row["ts"] + 30, ZoneInfo("UTC"))

        self.authenticate()
        with patch("webstats.api.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            body = self.client.get("/api/live?minutes=60&limit=20").get_json()
            private = self.client.get(
                "/api/live?minutes=60&limit=20&site=ad-fontes.app"
            ).get_json()
            scoped = self.client.get(
                "/api/live?minutes=60&limit=20&site=example.com"
            ).get_json()

        site_totals = {item["site"]: item["requests"] for item in body["sites"]}
        self.assertEqual(site_totals["ad-fontes.app"], 1)
        self.assertEqual(
            [(item["site"], item["path"]) for item in body["activity"]],
            [("ad-fontes.app", "/reader/private"), ("example.com", "/one")],
        )
        example_activity = next(
            item for item in body["activity"] if item["site"] == "example.com"
        )
        self.assertIn("new_page", example_activity["moments"])
        self.assertIn("traffic_record", example_activity["moments"])
        self.assertEqual(
            body["countries"],
            [
                {"country": "GB", "requests": 1, "visitors": 1},
                {"country": "US", "requests": 1, "visitors": 1},
            ],
        )
        self.assertEqual(
            body["totals"],
            {"requests": 2, "visitors": 2, "countries": 2, "sites": 2},
        )
        self.assertEqual(
            body["privacy"],
            {"excluded_activity_sites": []},
        )
        self.assertEqual(private["scope"], {"site": "ad-fontes.app"})
        self.assertEqual(private["sites"][0]["requests"], 1)
        self.assertEqual(private["activity"][0]["path"], "/reader/private")
        self.assertEqual(private["countries"][0]["country"], "GB")
        self.assertEqual(private["totals"]["requests"], 1)
        self.assertEqual(scoped["scope"], {"site": "example.com"})
        self.assertEqual([row["site"] for row in scoped["sites"]], ["example.com"])
        self.assertEqual(scoped["activity"][0]["site"], "example.com")

    def test_ad_fontes_page_analytics_are_not_suppressed(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = conn.execute(
                "INSERT INTO sites(name) VALUES ('ad-fontes.app')"
            ).lastrowid
            insert_requests(conn, [(
                "ad-fontes-page", "ad-fontes-page-fingerprint", site_id,
                1790586900, "2026-09-28", "reader", "GET", "/reader/essay",
                None, 200, 42, None, None, "Mozilla/5.0", "Other browser",
                "Other", 0, 0, "US",
            )])
            recompute_day(conn, "2026-09-28")
            conn.commit()
        self.authenticate()

        events = self.client.get(
            "/api/events?from=2026-09-28&to=2026-09-28"
            "&site=ad-fontes.app"
        ).get_json()
        pages = self.client.get(
            "/api/site/ad-fontes.app/pages?from=2026-09-28&to=2026-09-28"
        ).get_json()
        journeys = self.client.get(
            "/api/journeys?from=2026-09-28&to=2026-09-28"
            "&site=ad-fontes.app"
        ).get_json()

        self.assertEqual(events["events"][0]["path"], "/reader/essay")
        self.assertEqual(pages["pages"][0]["path"], "/reader/essay")
        self.assertEqual(journeys["totals"]["sessions"], 1)
        self.assertFalse(journeys["privacy"]["protected"])

    def test_detail_endpoints_match_hand_count(self):
        self.authenticate()
        query = "from=2026-09-28&to=2026-09-28"
        pages = self.client.get(f"/api/site/example.com/pages?{query}").get_json()["pages"]
        self.assertEqual({row["path"] for row in pages}, {"/one", "/missing"})
        status = self.client.get(f"/api/site/example.com/status?{query}").get_json()
        self.assertEqual(status["statuses"], [{"status": 200, "requests": 1}, {"status": 404, "requests": 1}])
        self.assertEqual(status["top_404"], [{"path": "/missing", "requests": 1}])
        series = self.client.get(f"/api/site/example.com/timeseries?{query}").get_json()["series"]
        self.assertEqual(series[0]["requests"], 2)

    def test_site_timeseries_fills_daily_and_hourly_gaps(self):
        self.authenticate()
        daily = self.client.get(
            "/api/site/example.com/timeseries?from=2026-09-27&to=2026-09-29"
        ).get_json()["series"]
        self.assertEqual(
            [(row["bucket"], row["requests"]) for row in daily],
            [("2026-09-27", 0), ("2026-09-28", 2), ("2026-09-29", 0)],
        )
        hourly = self.client.get(
            "/api/site/example.com/timeseries?from=2026-09-28&to=2026-09-28&interval=hour"
        ).get_json()["series"]
        self.assertEqual(len(hourly), 24)
        self.assertEqual(sum(row["requests"] for row in hourly), 2)
        self.assertEqual(hourly[0]["bucket"], "2026-09-28T00:00:00-0500")

    def test_old_hourly_range_falls_back_to_retained_daily_data(self):
        old_day = (
            datetime.now(ZoneInfo(self.config.server.timezone)).date()
            - timedelta(days=self.config.storage.raw_retention_days + 1)
        ).isoformat()
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            conn.execute(
                """
                INSERT INTO daily_filter(
                    site_id, day, include_bots, include_assets, requests,
                    unique_visitors, bytes, status_2xx, status_3xx,
                    status_4xx, status_5xx
                ) VALUES (?, ?, 0, 0, 7, 3, 700, 7, 0, 0, 0)
                """,
                (site_id, old_day),
            )
            conn.commit()

        self.authenticate()
        body = self.client.get(
            f"/api/site/example.com/timeseries?from={old_day}&to={old_day}&interval=hour"
        ).get_json()

        self.assertEqual(body["interval"], "day")
        self.assertEqual(body["series"], [{
            "bucket": old_day,
            "requests": 7,
            "unique_visitors": 3,
            "bytes": 700,
        }])

    def test_referrer_groups_use_host_boundaries_and_limit_after_grouping(self):
        self.assertEqual(_referrer_group("t.co"), "X / Twitter")
        self.assertEqual(_referrer_group("subdomain.x.com"), "X / Twitter")
        self.assertEqual(_referrer_group("www.google.com"), "Google")
        self.assertEqual(_referrer_group("google.es"), "Google")
        self.assertEqual(_referrer_group("news.google.com.br"), "Google")
        for host in (
            "reddit.com",
            "microsoft.com",
            "netflix.com",
            "box.com",
            "google.evil.com",
        ):
            self.assertEqual(_referrer_group(host), host)

        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            rows = [
                (site_id, "2026-09-28", f"source{index}.x.com", 1, 1, 1, 1)
                for index in range(30)
            ]
            rows.append((site_id, "2026-09-28", "reddit.com", 100, 100, 100, 100))
            conn.executemany(
                "INSERT INTO daily_referrer VALUES (?, ?, ?, ?, ?, ?, ?)", rows
            )
            conn.commit()

        self.authenticate()
        response = self.client.get(
            "/api/site/example.com/referrers?from=2026-09-28&to=2026-09-28"
        )
        self.assertEqual(
            response.get_json()["referrers"],
            [
                {"group": "reddit.com", "requests": 100},
                {"group": "X / Twitter", "requests": 30},
            ],
        )

    def test_event_journal_and_ai_crawler_field_guide(self):
        with self.log.open("a") as handle:
            handle.write(
                line(
                    "203.0.113.20",
                    "/linked-essay",
                    referrer="https://news.ycombinator.com/item?id=1",
                )
            )
            handle.write(
                line(
                    "203.0.113.21",
                    "/linked-essay",
                    ua="GPTBot/1.2 (+https://openai.com/gptbot)",
                )
            )
            handle.write(
                line(
                    "203.0.113.21",
                    "/ai-style.css",
                    ua="GPTBot/1.2 (+https://openai.com/gptbot)",
                )
            )
        ingest_once(self.config)
        self.authenticate()

        events = self.client.get(
            "/api/events?from=2026-09-28&to=2026-09-28"
        ).get_json()["events"]
        self.assertEqual(
            {(event["kind"], event["path"]) for event in events},
            {
                ("new_page", "/one"),
                ("new_page", "/linked-essay"),
                ("new_referrer", "/linked-essay"),
                ("first_ai_visit", "/linked-essay"),
            },
        )
        referrer = next(
            event for event in events if event["kind"] == "new_referrer"
        )
        self.assertEqual(referrer["source"], "news.ycombinator.com")
        scoped = self.client.get(
            "/api/events?from=2026-09-28&to=2026-09-28&site=example.com"
        ).get_json()
        self.assertEqual(scoped["site"], "example.com")
        self.assertEqual(len(scoped["events"]), len(events))
        self.assertEqual(
            self.client.get("/api/events?site=unknown.example").status_code,
            404,
        )
        inbox = self.client.get(
            "/api/inbox?from=2026-09-28&to=2026-09-28"
        ).get_json()
        self.assertEqual(inbox["counts"], {
            "all": 4, "discovery": 3, "readers": 1, "momentum": 0,
        })
        self.assertEqual(
            {item["category"] for item in inbox["events"]},
            {"discovery", "readers"},
        )
        discovery = self.client.get(
            "/api/inbox?from=2026-09-28&to=2026-09-28"
            "&site=example.com&category=discovery"
        ).get_json()
        self.assertEqual(len(discovery["events"]), 3)
        self.assertTrue(all(
            item["category"] == "discovery" for item in discovery["events"]
        ))
        self.assertEqual(
            self.client.get("/api/inbox?category=unknown").status_code,
            400,
        )

        body = self.client.get(
            "/api/ai-crawlers?from=2026-09-28&to=2026-09-28"
        ).get_json()
        self.assertEqual(
            body["totals"],
            {"requests": 1, "agents": 1, "pages": 1, "sites": 1},
        )
        self.assertEqual(body["sightings"][0]["agent"], "GPTBot")
        self.assertEqual(body["sightings"][0]["provider"], "OpenAI")
        self.assertEqual(body["sightings"][0]["path"], "/linked-essay")
        with_assets = self.client.get(
            "/api/ai-crawlers?from=2026-09-28&to=2026-09-28&assets=1"
        ).get_json()
        self.assertEqual(with_assets["totals"]["requests"], 2)
        self.assertEqual(with_assets["totals"]["pages"], 2)
        scoped_ai = self.client.get(
            "/api/ai-crawlers?from=2026-09-28&to=2026-09-28"
            "&site=example.com"
        ).get_json()
        self.assertEqual(scoped_ai["scope"], {"site": "example.com"})
        self.assertEqual(scoped_ai["totals"], body["totals"])

        page = self.client.get(
            "/api/site/example.com/page?path=%2Flinked-essay"
            "&from=2026-09-28&to=2026-09-28"
        ).get_json()
        self.assertEqual(page["totals"]["requests"], 1)
        self.assertEqual(page["totals"]["unique_visitors"], 1)
        self.assertEqual(page["totals"]["first_seen"], "2026-09-28")
        self.assertEqual(page["totals"]["last_seen"], "2026-09-28")
        self.assertEqual(
            page["referrers"],
            [{
                "group": "news.ycombinator.com",
                "requests": 1,
                "first_seen": "2026-09-28",
                "last_seen": "2026-09-28",
            }],
        )
        self.assertEqual(page["statuses"], [{"status": 200, "requests": 1}])
        self.assertEqual(page["ai_agents"][0]["agent"], "GPTBot")
        self.assertEqual(page["ai_agents"][0]["requests"], 1)
        self.assertEqual(page["countries"], [])

        with_bots = self.client.get(
            "/api/site/example.com/page?path=%2Flinked-essay"
            "&from=2026-09-28&to=2026-09-28&bots=1"
        ).get_json()
        self.assertEqual(with_bots["totals"]["requests"], 2)
        self.assertEqual(with_bots["statuses"], [{"status": 200, "requests": 2}])

        self.assertEqual(
            self.client.get("/api/site/example.com/page?path=relative").status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/api/site/example.com/page?path=%2Funknown").status_code,
            404,
        )

    def test_link_atlas_classifies_sources_and_maps_destinations(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            referrers = (
                ("2026-09-28", "debut.example", 4),
                ("2026-09-20", "rise.example", 1),
                ("2026-09-28", "rise.example", 3),
                ("2026-08-01", "return.example", 2),
                ("2026-09-28", "return.example", 2),
                ("2026-09-20", "quiet.example", 5),
                ("2026-08-10", "loyal.example", 1),
                ("2026-08-20", "loyal.example", 1),
                ("2026-09-01", "loyal.example", 1),
                ("2026-09-10", "loyal.example", 1),
                ("2026-09-28", "loyal.example", 1),
            )
            conn.executemany(
                """
                INSERT INTO daily_referrer(
                    site_id, day, referrer_host, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (site_id, day, source, count, count, count, count)
                    for day, source, count in referrers
                ],
            )
            conn.executemany(
                """
                INSERT INTO daily_page_referrer(
                    site_id, day, path, referrer_host, requests,
                    human_requests, nonasset_requests, human_nonasset_requests
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (site_id, "2026-09-28", "/launch", "debut.example", 4, 4, 4, 4),
                    (site_id, "2026-09-28", "/essay", "rise.example", 3, 3, 3, 3),
                    (site_id, "2026-09-28", "/archive", "return.example", 2, 2, 2, 2),
                    (site_id, "2026-09-28", "/home", "loyal.example", 1, 1, 1, 1),
                ),
            )
            conn.execute("INSERT INTO sites(name) VALUES ('ad-fontes.app')")
            conn.commit()
        self.authenticate()

        response = self.client.get(
            "/api/link-atlas?from=2026-09-22&to=2026-09-28"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        categories = {
            row["source"]: row["category"] for row in body["sources"]
        }
        self.assertEqual(categories["debut.example"], "debut")
        self.assertEqual(categories["rise.example"], "rising")
        self.assertEqual(categories["return.example"], "resurfaced")
        self.assertEqual(categories["loyal.example"], "loyal")
        self.assertEqual(categories["quiet.example"], "dormant")
        self.assertEqual(
            body["totals"],
            {
                "requests": 10,
                "active_sources": 4,
                "new_sources": 1,
                "linked_pages": 4,
                "sites": 1,
            },
        )
        self.assertEqual(body["series"][-1], {
            "bucket": "2026-09-28", "requests": 10,
        })
        launch = next(
            row for row in body["relationships"] if row["path"] == "/launch"
        )
        self.assertEqual(launch["source"], "debut.example")
        self.assertEqual(launch["requests"], 4)

        filtered = self.client.get(
            "/api/link-atlas?from=2026-09-22&to=2026-09-28"
            "&site=example.com&source=rise.example"
        ).get_json()
        self.assertEqual(filtered["scope"], {
            "site": "example.com", "source": "rise.example",
        })
        self.assertEqual(filtered["totals"]["requests"], 3)
        self.assertEqual(
            {row["source"] for row in filtered["relationships"]},
            {"rise.example"},
        )
        private = self.client.get(
            "/api/link-atlas?site=ad-fontes.app"
        ).get_json()
        self.assertFalse(private["privacy"]["protected"])
        self.assertEqual(private["sources"], [])
        private_inbox = self.client.get(
            "/api/inbox?site=ad-fontes.app"
        ).get_json()
        self.assertFalse(private_inbox["privacy"]["protected"])
        self.assertEqual(private_inbox["events"], [])
        self.assertEqual(
            self.client.get("/api/link-atlas?source=missing.example").status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/api/link-atlas?source=https://bad.example").status_code,
            400,
        )

    def test_feed_reader_observations_and_reported_counts(self):
        with self.log.open("a") as handle:
            handle.write(
                line(
                    "203.0.113.30",
                    "/feed.xml",
                    ua=(
                        "Inoreader/1.0 "
                        "(+http://www.inoreader.com/feed-fetcher; 3 subscribers; )"
                    ),
                )
            )
            handle.write(
                line("203.0.113.31", "/feed.xml", ua="Feedly/1.0")
            )
            handle.write(
                line("203.0.113.32", "/atom.xml", ua="FreshRSS/1.24.3")
            )
            handle.write(
                line(
                    "203.0.113.30",
                    "/rss.xml",
                    ua=(
                        "Inoreader/1.0 "
                        "(+http://www.inoreader.com/feed-fetcher; 3 subscribers; )"
                    ),
                )
            )
        ingest_once(self.config)
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            conn.execute(
                """
                INSERT INTO daily_feed_reader(
                    site_id, day, path, reader, requests,
                    reported_subscribers, first_seen, last_seen
                ) VALUES (?, '2026-09-01', '/old.xml', 'OldReader', 1, 99, 1, 1)
                """,
                (site_id,),
            )
            conn.commit()
        self.authenticate()

        body = self.client.get(
            "/api/feed-readers?from=2026-09-28&to=2026-09-28"
        ).get_json()
        self.assertEqual(
            body["totals"],
            {
                "reported_subscribers": 3,
                "subscriber_change": 0,
                "readers": 3,
                "feeds": 3,
                "requests": 4,
                "reporting_feeds": 1,
            },
        )
        self.assertEqual(
            body["series"],
            [{"bucket": "2026-09-28", "requests": 4, "reported_subscribers": 3}],
        )
        sightings = {row["reader"]: row for row in body["sightings"]}
        self.assertEqual(sightings["Inoreader"]["latest_subscribers"], 3)
        self.assertEqual(sightings["Inoreader"]["peak_subscribers"], 3)
        self.assertIsNone(sightings["Feedly"]["latest_subscribers"])
        self.assertIsNone(sightings["FreshRSS"]["latest_subscribers"])
        scoped = self.client.get(
            "/api/feed-readers?from=2026-09-28&to=2026-09-28&site=example.com"
        ).get_json()
        self.assertEqual(scoped["scope"], {"site": "example.com"})
        self.assertEqual(scoped["totals"], body["totals"])
        extended = self.client.get(
            "/api/feed-readers?from=2026-09-01&to=2026-09-28"
        ).get_json()
        self.assertEqual(extended["totals"]["reported_subscribers"], 3)
        self.assertEqual(extended["totals"]["reporting_feeds"], 1)
        self.assertEqual(
            self.client.get(
                "/api/feed-readers?site=unknown.example"
            ).status_code,
            404,
        )

    def test_page_galaxy_combines_links_journeys_and_ai_attention(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            conn.executemany(
                """
                INSERT INTO daily_page_status(
                    site_id, day, path, status, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, '2026-09-28', ?, 200, ?, ?, ?, ?)
                """,
                (
                    (site_id, "/galaxy-one", 8, 8, 8, 8),
                    (site_id, "/galaxy-two", 5, 5, 5, 5),
                ),
            )
            conn.executemany(
                """
                INSERT INTO daily_page_referrer(
                    site_id, day, path, referrer_host, requests,
                    human_requests, nonasset_requests, human_nonasset_requests
                ) VALUES (?, '2026-09-28', ?, ?, ?, ?, ?, ?)
                """,
                (
                    (site_id, "/galaxy-one", "news.example", 4, 4, 4, 4),
                    (site_id, "/galaxy-two", "news.example", 2, 2, 2, 2),
                ),
            )
            conn.execute(
                """
                INSERT INTO daily_page_agent(
                    site_id, day, path, ua_family, is_bot,
                    requests, asset_requests
                ) VALUES (?, '2026-09-28', '/galaxy-two', 'GPTBot', 1, 3, 0)
                """,
                (site_id,),
            )
            conn.executemany(
                """
                INSERT INTO daily_journey_endpoint(
                    site_id, day, path, entrances, exits, single_page_sessions
                ) VALUES (?, '2026-09-28', ?, ?, ?, ?)
                """,
                (
                    (site_id, "/galaxy-one", 3, 1, 1),
                    (site_id, "/galaxy-two", 1, 3, 1),
                ),
            )
            conn.execute(
                """
                INSERT INTO daily_journey_transition(
                    site_id, day, from_path, to_path, transitions
                ) VALUES (?, '2026-09-28', '/galaxy-one', '/galaxy-two', 2)
                """,
                (site_id,),
            )
            conn.execute("INSERT INTO sites(name) VALUES ('ad-fontes.app')")
            conn.commit()
        self.authenticate()

        response = self.client.get(
            "/api/galaxy?from=2026-09-28&to=2026-09-28"
            "&site=example.com&limit=4"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["scope"], {"site": "example.com"})
        self.assertGreaterEqual(body["totals"]["pages"], 2)
        self.assertEqual(body["totals"]["sources"], 1)
        self.assertEqual(body["totals"]["ai_agents"], 1)
        self.assertEqual(
            {node["kind"] for node in body["nodes"]},
            {"page", "source", "ai"},
        )
        self.assertEqual(
            {edge["kind"] for edge in body["edges"]},
            {"referral", "journey", "ai"},
        )
        pages = {row["path"]: row for row in body["pages"]}
        self.assertEqual(pages["/galaxy-one"]["referrals"], 4)
        self.assertEqual(pages["/galaxy-one"]["transitions_out"], 2)
        self.assertEqual(pages["/galaxy-two"]["ai_requests"], 3)
        self.assertEqual(pages["/galaxy-two"]["entrances"], 1)
        private = self.client.get(
            "/api/galaxy?site=ad-fontes.app"
        ).get_json()
        self.assertFalse(private["privacy"]["protected"])
        self.assertEqual(private["nodes"], [])
        self.assertEqual(
            self.client.get("/api/galaxy?site=unknown.example").status_code,
            404,
        )

    def test_change_engine_compares_equal_windows_and_explains_drivers(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            conn.executemany(
                """
                INSERT INTO daily_filter(
                    site_id, day, include_bots, include_assets, requests,
                    unique_visitors, bytes, status_2xx, status_3xx,
                    status_4xx, status_5xx
                ) VALUES (?, '2026-09-27', ?, 0, ?, ?, 100, ?, 0, ?, 0)
                """,
                (
                    (site_id, 0, 4, 3, 3, 1),
                    (site_id, 1, 6, 4, 5, 1),
                ),
            )
            conn.executemany(
                """
                INSERT INTO daily_page_status(
                    site_id, day, path, status, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (site_id, "2026-09-27", "/old", 200, 4, 4, 4, 4),
                    (site_id, "2026-09-28", "/new", 200, 3, 3, 3, 3),
                ),
            )
            conn.executemany(
                """
                INSERT INTO daily_page_referrer(
                    site_id, day, path, referrer_host, requests,
                    human_requests, nonasset_requests, human_nonasset_requests
                ) VALUES (?, ?, '/new', ?, ?, ?, ?, ?)
                """,
                (
                    (site_id, "2026-09-27", "old.example", 2, 2, 2, 2),
                    (site_id, "2026-09-28", "news.example", 3, 3, 3, 3),
                ),
            )
            conn.executemany(
                """
                INSERT INTO daily_country(
                    site_id, day, country, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (site_id, "2026-09-27", "CA", 2, 2, 2, 2),
                    (site_id, "2026-09-28", "US", 3, 3, 3, 3),
                ),
            )
            conn.execute(
                """
                INSERT INTO daily_page_agent(
                    site_id, day, path, ua_family, is_bot, requests, asset_requests
                ) VALUES (?, '2026-09-28', '/new', 'GPTBot', 1, 3, 0)
                """,
                (site_id,),
            )
            conn.execute(
                """
                INSERT INTO daily_feed_reader(
                    site_id, day, path, reader, requests,
                    reported_subscribers, first_seen, last_seen
                ) VALUES (?, '2026-09-28', '/feed.xml', 'Feedly', 2, 4, 1, 1)
                """,
                (site_id,),
            )
            conn.commit()
        self.authenticate()

        response = self.client.get(
            "/api/changes?from=2026-09-28&to=2026-09-28&site=example.com"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(
            body["window"],
            {
                "from": "2026-09-28", "to": "2026-09-28", "days": 1,
                "previous_from": "2026-09-27", "previous_to": "2026-09-27",
            },
        )
        self.assertEqual(body["summary"]["requests"], 2)
        self.assertEqual(body["summary"]["previous_requests"], 4)
        self.assertEqual(body["summary"]["change_percent"], -50.0)
        pages = {(row["path"], row["category"]) for row in body["pages"]}
        self.assertIn(("/new", "new"), pages)
        self.assertIn(("/old", "vanished"), pages)
        self.assertEqual(body["audience"]["ai"]["change"], 3)
        self.assertEqual(body["audience"]["feeds"]["change"], 2)
        self.assertTrue(body["narrative"])
        self.assertEqual(
            self.client.get("/api/changes?site=unknown.example").status_code,
            404,
        )

    def test_ai_policy_compares_current_rules_with_observed_paths(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            conn.executemany(
                """
                INSERT INTO daily_page_agent(
                    site_id, day, path, ua_family, is_bot, requests, asset_requests
                ) VALUES (?, '2026-09-28', ?, ?, 1, ?, 0)
                """,
                (
                    (site_id, "/private", "GPTBot", 3),
                    (site_id, "/private/public", "GPTBot", 2),
                    (site_id, "/tmp/file", "ClaudeBot", 1),
                ),
            )
            conn.commit()
        self.authenticate()
        robots = """User-agent: GPTBot
Disallow: /private
Allow: /private/public

User-agent: *
Disallow: /tmp
"""
        fetched = {
            "example.com": {
                "site": "example.com", "url": "https://example.com/robots.txt",
                "status": "available", "text": robots,
            }
        }
        with patch("webstats.api.fetch_robots_policies", return_value=fetched):
            response = self.client.get(
                "/api/ai-policy?from=2026-09-28&to=2026-09-28&site=example.com"
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["totals"]["observed_requests"], 6)
        self.assertEqual(body["totals"]["conflict_requests"], 4)
        agents = {row["agent"]: row for row in body["agents"]}
        self.assertEqual(agents["GPTBot"]["policy_source"], "explicit")
        self.assertEqual(agents["GPTBot"]["conflict_requests"], 3)
        self.assertEqual(agents["ClaudeBot"]["policy_source"], "wildcard")
        self.assertEqual(agents["ClaudeBot"]["conflict_requests"], 1)

    def test_content_observatory_combines_sitemaps_coverage_and_policy(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            conn.execute(
                """
                INSERT INTO daily_page_status(
                    site_id, day, path, status, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, '2026-09-28', '/outside', 200, 4, 4, 4, 4)
                """,
                (site_id,),
            )
            conn.executemany(
                """
                INSERT INTO daily_page_agent(
                    site_id, day, path, ua_family, is_bot, requests, asset_requests
                ) VALUES (?, '2026-09-28', ?, ?, 1, ?, 0)
                """,
                (
                    (site_id, "/one", "Googlebot", 2),
                    (site_id, "/ai-only", "GPTBot", 3),
                    (site_id, "/private", "GPTBot", 2),
                ),
            )
            conn.execute("INSERT INTO sites(name) VALUES ('ad-fontes.app')")
            conn.commit()
        self.authenticate()
        robots_text = """User-agent: GPTBot
Disallow: /private

User-agent: *
Disallow: /blocked
"""
        robots = {
            "example.com": {
                "site": "example.com", "url": "https://example.com/robots.txt",
                "status": "available", "text": robots_text,
            }
        }
        inventory = {
            "example.com": {
                "site": "example.com", "status": "available",
                "declared": ["https://example.com/sitemap.xml"],
                "documents": ["https://example.com/sitemap.xml"],
                "pages": [
                    {"path": "/one", "url": "https://example.com/one", "lastmod": "2026-09-28", "sitemap": "https://example.com/sitemap.xml"},
                    {"path": "/dark", "url": "https://example.com/dark", "lastmod": None, "sitemap": "https://example.com/sitemap.xml"},
                    {"path": "/ai-only", "url": "https://example.com/ai-only", "lastmod": None, "sitemap": "https://example.com/sitemap.xml"},
                    {"path": "/private", "url": "https://example.com/private", "lastmod": None, "sitemap": "https://example.com/sitemap.xml"},
                ],
                "errors": [], "limited": False,
            }
        }
        with patch("webstats.api.fetch_robots_policies", return_value=robots), patch(
            "webstats.api.fetch_sitemap_inventories", return_value=inventory
        ):
            response = self.client.get(
                "/api/content-observatory?from=2026-09-28&to=2026-09-28"
                "&site=example.com"
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["totals"]["published_pages"], 4)
        self.assertEqual(body["totals"]["dark_pages"], 3)
        self.assertEqual(body["totals"]["off_sitemap_pages"], 1)
        self.assertEqual(body["totals"]["policy_conflicts"], 1)
        self.assertEqual(body["totals"]["human_coverage_percent"], 25.0)
        self.assertEqual(body["totals"]["search_coverage_percent"], 25.0)
        self.assertEqual(body["totals"]["ai_coverage_percent"], 50.0)
        states = {row["path"]: row["state"] for row in body["dark_matter"]}
        self.assertEqual(states["/dark"], "unseen")
        self.assertEqual(states["/ai-only"], "crawler-only")
        self.assertEqual(body["off_sitemap"][0]["path"], "/outside")
        self.assertEqual(body["policy_conflicts"][0]["agents"], ["GPTBot"])
        crawlers = {(row["agent"], row["site"]): row for row in body["crawlers"]}
        self.assertEqual(crawlers[("Googlebot", "example.com")]["coverage_percent"], 25.0)
        self.assertEqual(crawlers[("GPTBot", "example.com")]["coverage_percent"], 50.0)

        private_robots = {
            "ad-fontes.app": {
                "site": "ad-fontes.app",
                "url": "https://ad-fontes.app/robots.txt",
                "status": "not_found", "text": "",
            }
        }
        private_inventory = {
            "ad-fontes.app": {
                "site": "ad-fontes.app", "status": "available",
                "declared": [], "documents": ["https://ad-fontes.app/sitemap.xml"],
                "pages": [{
                    "path": "/private-reading", "url": "https://ad-fontes.app/private-reading",
                    "lastmod": None, "sitemap": "https://ad-fontes.app/sitemap.xml",
                }],
                "errors": [], "limited": False,
            }
        }
        with patch(
            "webstats.api.fetch_robots_policies", return_value=private_robots
        ), patch(
            "webstats.api.fetch_sitemap_inventories", return_value=private_inventory
        ):
            private = self.client.get(
                "/api/content-observatory?site=ad-fontes.app"
            ).get_json()
        self.assertFalse(private["privacy"]["protected"])
        self.assertEqual(private["totals"]["published_pages"], 1)
        self.assertEqual(private["totals"]["analyzed_pages"], 1)
        self.assertEqual(private["dark_matter"][0]["path"], "/private-reading")

    def test_attention_episodes_reconstruct_and_attribute_a_burst(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            baseline_start = datetime(2026, 8, 23).date()
            rows = []
            for offset in range(28):
                day = (baseline_start + timedelta(days=offset)).isoformat()
                rows.append((site_id, day, 2, 2, 200, 2, 0))
            rows.extend((
                (site_id, "2026-09-20", 12, 7, 1200, 10, 2),
                (site_id, "2026-09-21", 6, 5, 600, 6, 0),
            ))
            for offset in range(8):
                day = (datetime(2026, 9, 22).date() + timedelta(days=offset)).isoformat()
                rows.append((site_id, day, 2, 2, 200, 2, 0))
            conn.executemany(
                """
                INSERT OR REPLACE INTO daily_filter(
                    site_id, day, include_bots, include_assets, requests,
                    unique_visitors, bytes, status_2xx, status_3xx,
                    status_4xx, status_5xx
                ) VALUES (?, ?, 0, 0, ?, ?, ?, ?, 0, ?, 0)
                """,
                rows,
            )
            conn.executemany(
                """
                INSERT INTO daily_page_status(
                    site_id, day, path, status, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (site_id, "2026-09-19", "/launch", 200, 2, 2, 2, 2),
                    (site_id, "2026-09-20", "/launch", 200, 10, 10, 10, 10),
                    (site_id, "2026-09-20", "/missing", 404, 2, 2, 2, 2),
                    (site_id, "2026-09-21", "/launch", 200, 6, 6, 6, 6),
                ),
            )
            conn.executemany(
                """
                INSERT INTO daily_page_referrer(
                    site_id, day, path, referrer_host, requests,
                    human_requests, nonasset_requests, human_nonasset_requests
                ) VALUES (?, ?, '/launch', 'news.example', ?, ?, ?, ?)
                """,
                (
                    (site_id, "2026-09-19", 1, 1, 1, 1),
                    (site_id, "2026-09-20", 8, 8, 8, 8),
                ),
            )
            conn.execute(
                """
                INSERT INTO daily_country(
                    site_id, day, country, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, '2026-09-20', 'US', 10, 10, 10, 10)
                """,
                (site_id,),
            )
            conn.execute(
                """
                INSERT INTO daily_page_agent(
                    site_id, day, path, ua_family, is_bot, requests, asset_requests
                ) VALUES (?, '2026-09-20', '/launch', 'GPTBot', 1, 4, 0)
                """,
                (site_id,),
            )
            conn.execute(
                """
                INSERT INTO events(
                    site_id, kind, event_key, occurred_at, day, path, source
                ) VALUES (?, 'new_referrer', 'episode-referrer', 1790000000,
                          '2026-09-20', '/launch', 'news.example')
                """,
                (site_id,),
            )
            conn.execute("INSERT INTO sites(name) VALUES ('ad-fontes.app')")
            conn.commit()
        self.authenticate()

        response = self.client.get(
            "/api/episodes?from=2026-09-20&to=2026-09-20&site=example.com"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["summary"]["episodes"], 1)
        episode = body["episodes"][0]
        self.assertEqual(episode["start"], "2026-09-20")
        self.assertEqual(episode["end"], "2026-09-21")
        self.assertEqual(episode["peak_requests"], 12)
        self.assertEqual(episode["kind"], "referral-wave")
        self.assertEqual(episode["drivers"]["pages"][0]["path"], "/launch")
        self.assertEqual(
            episode["drivers"]["referrers"][0]["source"], "news.example"
        )
        self.assertEqual(episode["drivers"]["errors"]["requests"], 2)
        self.assertEqual(episode["drivers"]["crawlers"][0]["agent"], "GPTBot")
        self.assertEqual(episode["lasting_effect"], "returned")
        self.assertTrue(episode["narrative"])

        private = self.client.get(
            "/api/episodes?site=ad-fontes.app"
        ).get_json()
        self.assertFalse(private["privacy"]["protected"])
        self.assertEqual(private["episodes"], [])

    def test_reliability_detects_regressions_bursts_and_scanner_noise(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            first = datetime(2026, 8, 23).date()
            baseline = []
            for offset in range(28):
                day = (first + timedelta(days=offset)).isoformat()
                baseline.append((
                    site_id, day, 2, 2, 200, 100, 100, 100,
                    2000, 1000, 1000, 0, 0, 1, 1,
                ))
            baseline.extend((
                (site_id, "2026-09-20", 6, 6, 4800, 400, 900, 1200,
                 600000, 100000, 140000, 4, 1, 9, 9),
                (site_id, "2026-09-21", 4, 4, 2400, 350, 700, 800,
                 400000, 100000, 120000, 3, 0, 5, 5),
                (site_id, "2026-09-22", 2, 2, 400, 150, 200, 220,
                 60000, 30000, 35000, 0, 0, 1, 1),
            ))
            conn.executemany(
                """
                INSERT INTO daily_reliability(
                    site_id, day, requests, timed_requests, total_ms,
                    p50_ms, p95_ms, max_ms, total_bytes, avg_bytes,
                    max_bytes, app_4xx, app_5xx, scanner_requests, scanner_errors
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                baseline,
            )
            conn.executemany(
                """
                INSERT INTO daily_performance(
                    site_id, day, path, requests, timed_requests, total_ms,
                    p50_ms, p95_ms, max_ms, total_bytes, avg_bytes, max_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (site_id, "2026-09-17", "/slow", 3, 3, 450, 120, 200, 220,
                     90000, 30000, 31000),
                    (site_id, "2026-09-20", "/slow", 3, 3, 2400, 400, 900, 1200,
                     300000, 100000, 140000),
                ),
            )
            conn.executemany(
                """
                INSERT INTO daily_page_status(
                    site_id, day, path, status, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, '2026-09-20', ?, ?, ?, ?, ?, ?)
                """,
                (
                    (site_id, "/broken", 500, 4, 4, 4, 4),
                    (site_id, "/.env", 404, 9, 9, 9, 9),
                ),
            )
            conn.execute("INSERT INTO sites(name) VALUES ('ad-fontes.app')")
            conn.commit()
        self.authenticate()

        response = self.client.get(
            "/api/reliability?from=2026-09-20&to=2026-09-22&site=example.com"
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["summary"]["app_5xx"], 1)
        self.assertEqual(body["summary"]["scanner_requests"], 15)
        kinds = {item["kind"] for item in body["incidents"]}
        self.assertIn("error-burst", kinds)
        self.assertIn("latency-regression", kinds)
        self.assertIn("size-anomaly", kinds)
        self.assertEqual(body["error_paths"][0]["path"], "/broken")
        self.assertEqual(body["probe_error_requests"], 9)
        self.assertEqual(body["pages"][0]["path"], "/slow")

        private = self.client.get(
            "/api/reliability?site=ad-fontes.app"
        ).get_json()
        self.assertFalse(private["privacy"]["protected"])
        self.assertEqual(private["incidents"], [])

    def test_almanac_records_streaks_milestones_and_calendar(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            conn.executemany(
                """
                INSERT INTO daily_filter(
                    site_id, day, include_bots, include_assets, requests,
                    unique_visitors, bytes, status_2xx, status_3xx,
                    status_4xx, status_5xx
                ) VALUES (?, ?, 0, 0, ?, ?, ?, ?, 0, 0, 0)
                """,
                (
                    (site_id, "2026-09-26", 40, 40, 4000, 40),
                    (site_id, "2026-09-27", 30, 30, 3000, 30),
                    (site_id, "2026-09-29", 50, 50, 5000, 50),
                ),
            )
            conn.commit()
        self.authenticate()

        with patch("webstats.api.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(
                2026, 9, 29, 12, tzinfo=ZoneInfo("America/Chicago")
            )
            response = self.client.get("/api/almanac?year=2026")
            site_response = self.client.get(
                "/api/almanac?year=2026&site=example.com"
            )
            invalid_year = self.client.get("/api/almanac?year=1999")
            unknown_site = self.client.get(
                "/api/almanac?site=unknown.example"
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["scope"], {"site": None, "label": "All sites"})
        self.assertEqual(
            body["totals"],
            {
                "requests": 122,
                "visitor_days": 122,
                "active_days": 4,
                "first_seen": "2026-09-26",
                "last_seen": "2026-09-29",
            },
        )
        self.assertEqual(body["records"]["busiest_day"]["day"], "2026-09-29")
        self.assertEqual(body["records"]["best_week"]["requests"], 70)
        self.assertEqual(body["records"]["best_week"]["period"], "2026-09-21")
        self.assertEqual(body["records"]["best_month"]["period"], "2026-09")
        self.assertEqual(body["records"]["current"], 4)
        self.assertEqual(body["records"]["longest"], 4)
        self.assertEqual(body["records"]["longest_start"], "2026-09-26")
        self.assertEqual(body["records"]["longest_end"], "2026-09-29")
        self.assertEqual(body["milestones"]["reached"], [{"value": 100, "day": "2026-09-29"}])
        self.assertEqual(body["milestones"]["next"], 250)
        self.assertEqual(body["milestones"]["progress"], 48.8)
        self.assertEqual(len(body["days"]), 365)
        days = {row["day"]: row for row in body["days"]}
        self.assertEqual(days["2026-09-25"]["requests"], 0)
        self.assertEqual(days["2026-09-29"]["visitor_days"], 50)
        self.assertEqual(
            [row["day"] for row in body["record_breakers"]],
            ["2026-09-29", "2026-09-26"],
        )

        site = site_response.get_json()
        self.assertEqual(site["scope"], {"site": "example.com", "label": "example.com"})
        self.assertEqual(invalid_year.status_code, 400)
        self.assertEqual(unknown_site.status_code, 404)

        with connect(self.config.storage.db_path) as conn:
            conn.execute(
                "DELETE FROM daily_filter WHERE day='2026-09-29' "
                "AND include_bots=0 AND include_assets=0"
            )
            conn.commit()
        with patch("webstats.api.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(
                2026, 9, 29, 12, tzinfo=ZoneInfo("America/Chicago")
            )
            through_yesterday = self.client.get(
                "/api/almanac?year=2026"
            ).get_json()
        self.assertEqual(through_yesterday["records"]["current"], 3)

    def test_weekly_briefing_compares_matching_days_and_archives_weeks(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            conn.execute(
                """
                INSERT INTO daily_filter(
                    site_id, day, include_bots, include_assets, requests,
                    unique_visitors, bytes, status_2xx, status_3xx,
                    status_4xx, status_5xx
                ) VALUES (?, '2026-09-21', 0, 0, 5, 4, 500, 5, 0, 0, 0)
                """,
                (site_id,),
            )
            conn.executemany(
                """
                INSERT INTO daily_page_status(
                    site_id, day, path, status, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, ?, ?, 200, ?, ?, ?, ?)
                """,
                (
                    (site_id, "2026-09-21", "/one", 1, 1, 1, 1),
                    (site_id, "2026-09-21", "/old", 2, 2, 2, 2),
                ),
            )
            conn.execute(
                """
                INSERT INTO daily_page_agent(
                    site_id, day, path, ua_family, is_bot, requests, asset_requests
                ) VALUES (?, '2026-09-28', '/one', 'GPTBot', 1, 3, 0)
                """,
                (site_id,),
            )
            conn.execute(
                """
                INSERT INTO daily_country(
                    site_id, day, country, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, '2026-09-28', 'US', 1, 1, 1, 1)
                """,
                (site_id,),
            )
            conn.executemany(
                """
                INSERT INTO daily_feed_reader(
                    site_id, day, path, reader, requests,
                    reported_subscribers, first_seen, last_seen
                ) VALUES (?, ?, '/feed.xml', 'Inoreader', 1, ?, 1, 1)
                """,
                (
                    (site_id, "2026-09-21", 3),
                    (site_id, "2026-09-28", 5),
                ),
            )
            conn.execute(
                """
                INSERT INTO events(
                    site_id, kind, event_key, occurred_at, day, path
                ) VALUES (?, 'new_page', 'briefing-new-page', 1790600000,
                          '2026-09-28', '/new')
                """,
                (site_id,),
            )
            conn.commit()
        self.authenticate()

        with patch("webstats.api.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(
                2026, 9, 29, 12, tzinfo=ZoneInfo("America/Chicago")
            )
            default = self.client.get("/api/briefing")
            response = self.client.get("/api/briefing?week=2026-09-28")
            archived = self.client.get("/api/briefing?week=2026-09-21")
            invalid = self.client.get("/api/briefing?week=2026-09-22")
            future = self.client.get("/api/briefing?week=2026-10-05")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(default.get_json()["week"]["from"], "2026-09-21")
        body = response.get_json()
        self.assertEqual(
            body["week"],
            {
                "from": "2026-09-28",
                "to": "2026-09-29",
                "scheduled_to": "2026-10-04",
                "complete": False,
                "days_compared": 2,
                "previous_from": "2026-09-21",
                "previous_to": "2026-09-22",
            },
        )
        self.assertEqual(body["summary"]["requests"], 2)
        self.assertEqual(body["summary"]["previous_requests"], 5)
        self.assertEqual(body["summary"]["request_change_percent"], -60.0)
        self.assertEqual(body["discoveries"]["pages"], 2)
        self.assertEqual(body["ai"], {"requests": 3, "agents": 1, "pages": 1})
        self.assertEqual(body["geography"]["new_countries"], ["US"])
        self.assertEqual(body["feeds"]["reported_subscribers"], 5)
        self.assertEqual(body["feeds"]["change"], 2)
        scoped = self.client.get(
            "/api/briefing?week=2026-09-28&site=example.com"
        ).get_json()
        self.assertEqual(scoped["scope"], {"site": "example.com"})
        self.assertEqual(scoped["summary"]["requests"], 2)
        self.assertEqual(
            [week["week"] for week in body["available_weeks"]],
            ["2026-09-28", "2026-09-21"],
        )
        old_page = next(row for row in body["page_changes"] if row["path"] == "/old")
        self.assertEqual(old_page["change"], -2)
        self.assertTrue(body["narrative"])
        self.assertTrue(archived.get_json()["week"]["complete"])
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(future.status_code, 400)

    def test_content_pulse_classifies_page_momentum_and_context(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            status_rows = []
            for day, path, requests in (
                ("2026-08-01", "/revived", 2),
                ("2026-09-28", "/revived", 1),
                ("2026-09-20", "/rising", 1),
                ("2026-09-28", "/rising", 5),
                ("2026-09-01", "/cooling", 16),
                ("2026-09-28", "/cooling", 1),
                ("2026-07-01", "/dormant", 3),
            ):
                status_rows.append(
                    (site_id, day, path, 200, requests, requests, requests, requests)
                )
            conn.executemany(
                """
                INSERT INTO daily_page_status(
                    site_id, day, path, status, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                status_rows,
            )
            conn.execute(
                """
                INSERT INTO daily_page_referrer(
                    site_id, day, path, referrer_host, requests,
                    human_requests, nonasset_requests, human_nonasset_requests
                ) VALUES (?, '2026-09-28', '/revived', 'example.org', 1, 1, 1, 1)
                """,
                (site_id,),
            )
            conn.execute(
                """
                INSERT INTO daily_page_country(
                    site_id, day, path, country, requests,
                    human_requests, nonasset_requests, human_nonasset_requests
                ) VALUES (?, '2026-09-28', '/revived', 'CA', 1, 1, 1, 1)
                """,
                (site_id,),
            )
            conn.execute(
                """
                INSERT INTO daily_page_agent(
                    site_id, day, path, ua_family, is_bot, requests, asset_requests
                ) VALUES (?, '2026-09-28', '/revived', 'GPTBot', 1, 2, 0)
                """,
                (site_id,),
            )
            conn.commit()
        self.authenticate()

        with patch("webstats.api.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(
                2026, 9, 29, 12, tzinfo=ZoneInfo("America/Chicago")
            )
            response = self.client.get("/api/pulse")
            scoped = self.client.get("/api/pulse?site=example.com")
            unknown = self.client.get("/api/pulse?site=unknown.example")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        pages = {row["path"]: row for row in body["signals"]}
        self.assertEqual(pages["/revived"]["category"], "resurfaced")
        self.assertEqual(pages["/revived"]["quiet_days"], 58)
        self.assertEqual(pages["/revived"]["top_referrer"], "example.org")
        self.assertEqual(pages["/revived"]["top_country"], "CA")
        self.assertEqual(pages["/revived"]["ai_requests"], 2)
        self.assertEqual(pages["/rising"]["category"], "rising")
        self.assertEqual(pages["/cooling"]["category"], "cooling")
        self.assertEqual(pages["/dormant"]["category"], "dormant")
        self.assertEqual(body["counts"]["debut"], 1)
        self.assertEqual(scoped.get_json()["scope"], {"site": "example.com"})
        self.assertEqual(unknown.status_code, 404)

    def test_error_intelligence_triages_misses_and_filters_probes(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            raw_rows = []
            for number, path, referrer, country in (
                (1, "/gone", "old.example", "CA"),
                (2, "/posts/webstat", None, "US"),
                (3, "/persistent", None, "GB"),
                (4, "/.env", None, "NL"),
            ):
                raw_rows.append((
                    f"error-source-{number}", f"error-fingerprint-{number}",
                    site_id, 1790672400 + number, "2026-09-29", f"visitor-{number}",
                    "GET", path, None, 404, 10, referrer,
                    f"https://{referrer}/link" if referrer else None,
                    "Mozilla/5.0", "Other browser", "Other", 0, 0, country,
                ))
            insert_requests(conn, raw_rows)
            recompute_day(conn, "2026-09-29")
            conn.executemany(
                """
                INSERT INTO daily_page_status(
                    site_id, day, path, status, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, ?, ?, 200, ?, ?, ?, ?)
                """,
                (
                    (site_id, "2026-09-20", "/gone", 4, 4, 4, 4),
                    (site_id, "2026-09-28", "/posts/webstats", 20, 20, 20, 20),
                ),
            )
            conn.executemany(
                """
                INSERT INTO daily_404(
                    site_id, day, path, requests, human_requests,
                    nonasset_requests, human_nonasset_requests
                ) VALUES (?, ?, '/persistent', 1, 1, 1, 1)
                """,
                ((site_id, "2026-09-27"), (site_id, "2026-09-28")),
            )
            conn.commit()
        self.authenticate()

        with patch("webstats.api.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(
                2026, 9, 29, 12, tzinfo=ZoneInfo("America/Chicago")
            )
            response = self.client.get("/api/errors?days=30")
            scoped = self.client.get("/api/errors?days=7&site=example.com")
            unknown = self.client.get("/api/errors?site=unknown.example")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        signals = {row["path"]: row for row in body["signals"]}
        self.assertEqual(signals["/gone"]["category"], "regression")
        self.assertEqual(signals["/gone"]["last_success"], "2026-09-20")
        self.assertEqual(signals["/gone"]["top_referrer"], "old.example")
        self.assertEqual(signals["/gone"]["top_country"], "CA")
        self.assertEqual(signals["/posts/webstat"]["category"], "typo")
        self.assertEqual(
            signals["/posts/webstat"]["suggestion"]["path"],
            "/posts/webstats",
        )
        self.assertEqual(signals["/persistent"]["category"], "persistent")
        self.assertEqual(signals["/persistent"]["active_days"], 3)
        self.assertEqual(body["summary"]["probe_requests"], 1)
        self.assertEqual(body["probes"][0]["path"], "/.env")
        self.assertEqual(body["window"]["days"], 30)
        self.assertEqual(scoped.get_json()["scope"], {"site": "example.com"})
        self.assertEqual(unknown.status_code, 404)

    def test_reading_paths_and_page_transitions(self):
        with connect(self.config.storage.db_path) as conn:
            site_id = site_id_map(conn)["example.com"]
            rows = []
            for index, path in enumerate(("/one", "/two", "/three")):
                rows.append((
                    f"path-source-{index}", f"path-fingerprint-{index}",
                    site_id, 1790586900 + index * 60, "2026-09-28",
                    "journey-reader", "GET", path, None, 200, 10, None,
                    None, "Mozilla/5.0", "Other browser", "Other", 0, 0, "US",
                ))
            insert_requests(conn, rows)
            conn.execute("INSERT INTO sites(name) VALUES ('ad-fontes.app')")
            private_id = site_id_map(conn)["ad-fontes.app"]
            insert_requests(conn, [
                (
                    "private-path-source", "private-path-fingerprint", private_id,
                    1790586900, "2026-09-28", "private-reader", "GET",
                    "/private", None, 200, 10, None, None, "Mozilla/5.0",
                    "Other browser", "Other", 0, 0, None,
                )
            ])
            recompute_day(conn, "2026-09-28")
            conn.commit()
        self.authenticate()

        response = self.client.get(
            "/api/journeys?from=2026-09-28&to=2026-09-28&site=example.com"
        )
        private = self.client.get(
            "/api/journeys?from=2026-09-28&to=2026-09-28&site=ad-fontes.app"
        )
        unknown = self.client.get("/api/journeys?site=unknown.example")
        page = self.client.get(
            "/api/site/example.com/page?from=2026-09-28&to=2026-09-28"
            "&path=/one"
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["scope"], {"site": "example.com"})
        self.assertEqual(body["totals"]["sessions"], 2)
        self.assertEqual(body["totals"]["multi_page_sessions"], 1)
        self.assertEqual(body["totals"]["average_depth"], 2.0)
        self.assertEqual(
            [(row["from_path"], row["to_path"]) for row in body["transitions"]],
            [("/one", "/two"), ("/two", "/three")],
        )
        page_journey = page.get_json()["journey"]
        self.assertEqual(page_journey["entrances"], 2)
        self.assertEqual(page_journey["next"][0]["path"], "/two")
        self.assertFalse(private.get_json()["privacy"]["protected"])
        self.assertEqual(private.get_json()["totals"]["sessions"], 1)
        self.assertEqual(unknown.status_code, 404)

    def test_invalid_range_is_rejected(self):
        self.authenticate()
        response = self.client.get("/api/overview?from=2026-09-29&to=2026-09-28")
        self.assertEqual(response.status_code, 400)

    def test_invalid_integer_is_rejected(self):
        self.authenticate()
        response = self.client.get("/api/live?minutes=forever")
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
