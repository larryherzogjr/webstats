from pathlib import Path
import tempfile
import unittest

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
            set_password(path, "a-long-test-password")
            text = path.read_text()
            self.assertIn('admin_user = "larry"', text)
            self.assertIn("# keep", text)
            encoded = text.split('admin_password_hash = "', 1)[1].split('"', 1)[0]
            self.assertTrue(bcrypt.checkpw(b"a-long-test-password", encoded.encode()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_rejects_short_password(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            path.write_text('[server]\nadmin_password_hash = "old"\n')
            with self.assertRaises(ValueError):
                set_password(path, "too-short")


if __name__ == "__main__":
    unittest.main()

