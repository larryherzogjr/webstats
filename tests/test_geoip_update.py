from datetime import datetime, timezone
import gzip
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.update_geoip import current_release, database_url, update


class GeoIPUpdateTests(unittest.TestCase):
    def test_release_and_url(self):
        release = current_release(datetime(2026, 9, 28, tzinfo=timezone.utc))
        self.assertEqual(release, "2026-09")
        self.assertEqual(
            database_url(release),
            "https://download.db-ip.com/free/dbip-country-lite-2026-09.mmdb.gz",
        )
        with self.assertRaises(ValueError):
            database_url("September")

    def test_update_is_atomic_and_skips_current_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "country.mmdb"
            stamp = root / "country.release"
            response = io.BytesIO(gzip.compress(b"validated database"))
            with patch("scripts.update_geoip.urlopen", return_value=response) as opener:
                with patch("scripts.update_geoip.validate_database") as validate:
                    self.assertTrue(update(database, stamp, "2026-09"))
                    self.assertEqual(database.read_bytes(), b"validated database")
                    self.assertEqual(stamp.read_text(), "2026-09\n")
                    self.assertFalse(update(database, stamp, "2026-09"))
            opener.assert_called_once()
            self.assertEqual(validate.call_count, 2)
            self.assertEqual(database.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
