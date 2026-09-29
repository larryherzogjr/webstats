import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bcrypt

from scripts.set_password import set_password


class PasswordToolTests(unittest.TestCase):
    def test_updates_only_password_hash_and_preserves_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            path.write_text(
                '[server]\nadmin_user = "larry"\nadmin_password_hash = "old" # keep\n'
            )
            path.chmod(0o640)
            original = path.stat()
            with patch("scripts.set_password.os.chown", wraps=os.chown) as chown:
                set_password(path, "a-long-test-password")
            text = path.read_text()
            self.assertIn('admin_user = "larry"', text)
            self.assertIn("# keep", text)
            encoded = text.split('admin_password_hash = "', 1)[1].split('"', 1)[0]
            self.assertTrue(bcrypt.checkpw(b"a-long-test-password", encoded.encode()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            chown.assert_called_once()
            self.assertEqual(chown.call_args.args[1:], (original.st_uid, original.st_gid))

    def test_accepts_short_password_without_complexity_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            path.write_text('[server]\nadmin_password_hash = "old"\n')
            set_password(path, "x")
            encoded = (
                path.read_text().split('admin_password_hash = "', 1)[1].split('"', 1)[0]
            )
            self.assertTrue(bcrypt.checkpw(b"x", encoded.encode()))

    def test_rejects_empty_password(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            path.write_text('[server]\nadmin_password_hash = "old"\n')
            with self.assertRaises(ValueError):
                set_password(path, "")


if __name__ == "__main__":
    unittest.main()
