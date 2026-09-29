from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeploymentConfigTests(unittest.TestCase):
    def test_nginx_log_policy_includes_only_selected_ordinary_hosts(self):
        text = (ROOT / "deploy/nginx-webstats-log.conf").read_text(encoding="utf-8")
        map_body = re.search(
            r"map \$host \$webstats_loggable \{(?P<body>.*?)\n\}", text, re.DOTALL
        ).group("body")

        included = {
            "larryherzogjr.com",
            "www.larryherzogjr.com",
            "pick5.ospdy.com",
            "hdu.ospdy.com",
            "euphonium.studio",
            "www.euphonium.studio",
            "alexis.tips",
            "www.alexis.tips",
            "herzogenclave.com",
            "www.herzogenclave.com",
        }
        for host in included:
            self.assertRegex(map_body, rf"(?m)^\s*{re.escape(host)}\s+1;")

        self.assertNotIn("ad-fontes.app", map_body)
        self.assertNotIn("fuse.ospdy.com", map_body)
        self.assertNotIn("wordfall.ospdy.com", map_body)

    def test_ad_fontes_has_private_query_free_format(self):
        text = (ROOT / "deploy/nginx-webstats-log.conf").read_text(encoding="utf-8")
        private_format = re.search(
            r"log_format webstats_private_host(?P<body>.*?);", text, re.DOTALL
        ).group("body")

        self.assertIn("$uri", private_format)
        self.assertNotIn("$request_uri", private_format)
        self.assertNotIn("$http_referer", private_format)
        self.assertIn("$request_time", private_format)

    def test_shared_format_records_request_time(self):
        text = (ROOT / "deploy/nginx-webstats-log.conf").read_text(encoding="utf-8")
        shared_format = re.search(
            r"log_format webstats_combined_host(?P<body>.*?);", text, re.DOTALL
        ).group("body")
        self.assertIn("$request_time", shared_format)

    def test_production_csp_has_no_inline_dashboard_styles(self):
        javascript = (ROOT / "webstats/static/dashboard.js").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('style="', javascript)

    def test_chronicle_service_has_a_hard_runtime_limit(self):
        unit = (ROOT / "deploy/webstats-chronicle.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("TimeoutStartSec=120", unit)

    def test_app_config_includes_ad_fontes_and_excludes_private_apps(self):
        text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        configured = set(re.findall(r'^name = "([^"]+)"$', text, re.MULTILINE))

        self.assertIn("ad-fontes.app", configured)
        self.assertNotIn("fuse.ospdy.com", configured)
        self.assertNotIn("wordfall.ospdy.com", configured)


if __name__ == "__main__":
    unittest.main()
