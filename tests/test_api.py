from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bcrypt

from webstats.app import create_app
from webstats.api import _referrer_group
from webstats.auth import _attempts, _credential_token
from webstats.config import Config, GeoIPConfig, ServerConfig, SiteConfig, StorageConfig
from webstats.db import connect, site_id_map
from webstats.ingest import ingest_once


def line(ip, path, status=200, ua="Mozilla/5.0 AppleWebKit/537.36 Chrome/128.0 Safari/537.36"):
    return (
        f'{ip} - - [28/Sep/2026:10:15:00 -0500] "GET {path} HTTP/1.1" '
        f'{status} 42 "-" "{ua}"\n'
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

    def test_referrer_groups_use_host_boundaries_and_limit_after_grouping(self):
        self.assertEqual(_referrer_group("t.co"), "X / Twitter")
        self.assertEqual(_referrer_group("subdomain.x.com"), "X / Twitter")
        self.assertEqual(_referrer_group("www.google.com"), "Google")
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
