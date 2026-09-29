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
        self.assertEqual(self.client.get("/pulse").status_code, 200)
        self.assertEqual(self.client.get("/errors").status_code, 200)
        self.assertEqual(self.client.get("/journeys").status_code, 200)
        self.assertEqual(self.client.get("/feed-readers").status_code, 200)
        self.assertEqual(self.client.get("/ai-crawlers").status_code, 200)
        self.assertEqual(self.client.get("/live").status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/site/not-configured.test").status_code, 404)

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

    def test_live_radar_activity_geography_and_private_site_exclusion(self):
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
            [("example.com", "/one")],
        )
        self.assertIn("new_page", body["activity"][0]["moments"])
        self.assertIn("traffic_record", body["activity"][0]["moments"])
        self.assertEqual(
            body["countries"],
            [{"country": "US", "requests": 1, "visitors": 1}],
        )
        self.assertEqual(
            body["totals"],
            {"requests": 1, "visitors": 1, "countries": 1, "sites": 1},
        )
        self.assertEqual(
            body["privacy"],
            {"excluded_activity_sites": ["ad-fontes.app"]},
        )
        self.assertEqual(private["scope"], {"site": "ad-fontes.app"})
        self.assertEqual(private["sites"][0]["requests"], 1)
        self.assertEqual(private["activity"], [])
        self.assertEqual(private["countries"], [])
        self.assertEqual(private["totals"]["requests"], 0)
        self.assertEqual(scoped["scope"], {"site": "example.com"})
        self.assertEqual([row["site"] for row in scoped["sites"]], ["example.com"])
        self.assertEqual(scoped["activity"][0]["site"], "example.com")

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
            [{"group": "news.ycombinator.com", "requests": 1}],
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
        ingest_once(self.config)
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
                "feeds": 2,
                "requests": 3,
                "reporting_feeds": 1,
            },
        )
        self.assertEqual(
            body["series"],
            [{"bucket": "2026-09-28", "requests": 3, "reported_subscribers": 3}],
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
        self.assertEqual(
            self.client.get(
                "/api/feed-readers?site=unknown.example"
            ).status_code,
            404,
        )

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
        self.assertTrue(private.get_json()["privacy"]["protected"])
        self.assertEqual(private.get_json()["totals"]["sessions"], 0)
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
