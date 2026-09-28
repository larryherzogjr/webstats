from pathlib import Path
import tempfile
import unittest

from webstats.config import load_config


class ConfigTests(unittest.TestCase):
    def test_shared_host_prefixed_log_can_serve_multiple_sites(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            config.write_text(
                f'''[server]
bind = "127.0.0.1:5010"
secret_key = "0123456789abcdef0123456789abcdef"
admin_user = "admin"
admin_password_hash = "$2b$12$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWX123456"
timezone = "America/Chicago"
[storage]
db_path = "{root / 'webstats.db'}"
state_path = "{root / 'state.json'}"
raw_retention_days = 90
[geoip]
enabled = false
db_path = "{root / 'geo.mmdb'}"
[privacy]
ip_hash_salt_rotation = "daily"
[logs]
format = "combined_host"
[[sites]]
name = "one.test"
paths = ["/var/log/nginx/webstats.access.log"]
[[sites]]
name = "two.test"
paths = ["/var/log/nginx/webstats.access.log"]
'''
            )
            loaded = load_config(config)
            self.assertEqual(loaded.log_format, "combined_host")
            self.assertEqual(len(loaded.sites), 2)


if __name__ == "__main__":
    unittest.main()
