import gzip
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.backfill import run
from webstats.config import load_config
from webstats.ingest import ingest_once
from tests.test_ingest import log_line


def write_config(root: Path, retention_days: int = 90) -> Path:
    base = root / "example.access.log"
    config = root / "config.toml"
    config.write_text(
        f'''[server]
bind = "127.0.0.1:5010"
secret_key = "0123456789abcdef0123456789abcdef"
admin_user = "admin"
admin_password_hash = "$2b$12$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWX123456"
timezone = "UTC"
[storage]
db_path = "{root / 'test.db'}"
state_path = "{root / 'state.json'}"
raw_retention_days = {retention_days}
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
    return config


class BackfillTests(unittest.TestCase):
    def test_gzip_history_is_imported_and_repeat_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "example.access.log"
            base.write_text(log_line("/current"))
            archive = Path(str(base) + ".2.gz")
            with gzip.open(archive, "wt") as handle:
                handle.write(log_line("/archived"))
            config = write_config(root)
            first = run(str(config))
            second = run(str(config))
            incremental = ingest_once(load_config(config))
            self.assertEqual(first.inserted, 2)
            self.assertEqual(second.inserted, 0)
            self.assertEqual(incremental.inserted, 0)
            with sqlite3.connect(root / "test.db") as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 2)

    def test_live_ingest_then_uncompressed_rotation_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "example.access.log"
            base.write_text(log_line("/live"))
            config_path = write_config(root)
            config = load_config(config_path)
            self.assertEqual(ingest_once(config).inserted, 1)
            base.rename(Path(str(base) + ".1"))
            base.write_text("")

            result = run(str(config_path))

            self.assertEqual(result.inserted, 0)
            with sqlite3.connect(root / "test.db") as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1
                )

    def test_rotated_tail_ingest_then_backfill_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "example.access.log"
            base.write_text(log_line("/before"))
            config_path = write_config(root)
            config = load_config(config_path)
            self.assertEqual(ingest_once(config).inserted, 1)

            with base.open("a") as handle:
                handle.write(log_line("/tail"))
            base.rename(Path(str(base) + ".1"))
            base.write_text(log_line("/after"))
            self.assertEqual(ingest_once(config).inserted, 2)
            with sqlite3.connect(root / "test.db") as conn:
                tail_source = conn.execute(
                    "SELECT source_key FROM requests WHERE path='/tail'"
                ).fetchone()[0]
            self.assertTrue(tail_source.startswith(f"{base}:"))

            result = run(str(config_path))

            self.assertEqual(result.inserted, 0)
            with sqlite3.connect(root / "test.db") as conn:
                paths = conn.execute(
                    "SELECT path, COUNT(*) FROM requests GROUP BY path ORDER BY path"
                ).fetchall()
            self.assertEqual(paths, [("/after", 1), ("/before", 1), ("/tail", 1)])

    def test_backfill_recognizes_legacy_rotated_tail_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "example.access.log"
            base.write_text(log_line("/before"))
            config_path = write_config(root)
            config = load_config(config_path)
            ingest_once(config)

            with base.open("a") as handle:
                handle.write(log_line("/tail"))
            rotated = Path(str(base) + ".1")
            base.rename(rotated)
            base.write_text("")
            ingest_once(config)

            with sqlite3.connect(root / "test.db") as conn:
                canonical = conn.execute(
                    "SELECT source_key FROM requests WHERE path='/tail'"
                ).fetchone()[0]
                legacy = str(rotated) + canonical[len(str(base)):]
                conn.execute(
                    "UPDATE requests SET source_key=? WHERE path='/tail'",
                    (legacy,),
                )
                conn.commit()

            result = run(str(config_path))

            self.assertEqual(result.inserted, 0)
            with sqlite3.connect(root / "test.db") as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 2
                )

    def test_live_ingest_then_gzip_rotation_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "example.access.log"
            content = log_line("/live")
            base.write_text(content)
            config_path = write_config(root)
            config = load_config(config_path)
            self.assertEqual(ingest_once(config).inserted, 1)
            archive = Path(str(base) + ".2.gz")
            with gzip.open(archive, "wt") as handle:
                handle.write(content)
            base.write_text("")

            first = run(str(config_path))
            second = run(str(config_path))

            self.assertEqual(first.inserted, 0)
            self.assertEqual(second.inserted, 0)
            with sqlite3.connect(root / "test.db") as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1
                )

    def test_old_complete_rollup_is_not_replaced_by_partial_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "example.access.log"
            old_line = log_line("/original").replace("28/Sep/2026", "27/Sep/2026")
            base.write_text(old_line)
            config_path = write_config(root, retention_days=1)
            run(str(config_path))
            with sqlite3.connect(root / "test.db") as conn:
                original = conn.execute(
                    "SELECT requests FROM daily_filter "
                    "WHERE day='2026-09-27' "
                    "AND include_bots=1 AND include_assets=1"
                ).fetchone()[0]
                self.assertEqual(original, 1)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0
                )

            archive = Path(str(base) + ".2.gz")
            with gzip.open(archive, "wt") as handle:
                handle.write(
                    log_line("/partial-addition").replace(
                        "28/Sep/2026", "27/Sep/2026"
                    )
                )
            result = run(str(config_path))

            self.assertEqual(result.inserted, 0)
            with sqlite3.connect(root / "test.db") as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT requests FROM daily_filter "
                        "WHERE day='2026-09-27' "
                        "AND include_bots=1 AND include_assets=1"
                    ).fetchone()[0],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
