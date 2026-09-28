import gzip
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.backfill import run
from tests.test_ingest import log_line


class BackfillTests(unittest.TestCase):
    def test_gzip_history_is_imported_and_repeat_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "example.access.log"
            archive = Path(str(base) + ".2.gz")
            with gzip.open(archive, "wt") as handle:
                handle.write(log_line("/archived"))
            config = root / "config.toml"
            config.write_text(
                f'''[server]
bind = "127.0.0.1:5010"
secret_key = "secret"
admin_user = "admin"
admin_password_hash = "hash"
timezone = "UTC"
[storage]
db_path = "{root / 'test.db'}"
state_path = "{root / 'state.json'}"
raw_retention_days = 90
[geoip]
enabled = false
db_path = "{root / 'geo.mmdb'}"
[privacy]
ip_hash_salt_rotation = "daily"
[logs]
format = "combined"
[[sites]]
name = "example.com"
paths = ["{base}"]
'''
            )
            first = run(str(config))
            second = run(str(config))
            self.assertEqual(first.inserted, 1)
            self.assertEqual(second.inserted, 0)
            with sqlite3.connect(root / "test.db") as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
