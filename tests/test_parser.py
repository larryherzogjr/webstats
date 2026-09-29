from pathlib import Path
import unittest

from webstats.logformat import COMBINED, compile_log_format
from webstats.parser import ParseError, parse_line


FIXTURES = Path(__file__).parent / "fixtures"


class ParserTests(unittest.TestCase):
    def test_combined_line_is_normalized(self):
        line = (FIXTURES / "combined.log").read_text().splitlines()[0]
        row = parse_line(line, compile_log_format("combined"), "site.test", "secret")
        self.assertEqual(row.method, "GET")
        self.assertEqual(row.path, "/essays/hello")
        self.assertEqual(row.query, "source=test")
        self.assertEqual(row.status, 200)
        self.assertEqual(row.referrer_host, "example.com")
        self.assertEqual(row.ua_family, "Chrome")
        self.assertEqual(row.os_family, "macOS")
        self.assertFalse(row.is_bot)
        self.assertEqual(len(row.ip_hash), 16)

    def test_host_prefixed_format(self):
        line = (FIXTURES / "combined_host.log").read_text().strip()
        row = parse_line(
            line, compile_log_format("combined_host"), "example.com", "secret"
        )
        self.assertEqual(row.host, "example.com")
        self.assertEqual(row.ua_family, "Safari")
        self.assertEqual(row.os_family, "iOS")

    def test_host_prefixed_self_referral_uses_line_host(self):
        line = (
            'other.test 203.0.113.10 - - [28/Sep/2026:10:15:00 -0500] '
            '"GET / HTTP/1.1" 200 42 "https://www.other.test/page" '
            '"Mozilla/5.0 Safari/537.36"'
        )
        row = parse_line(
            line, compile_log_format("combined_host"), "first.test", "secret"
        )
        self.assertIsNone(row.referrer_host)

    def test_explicit_format(self):
        compiled = compile_log_format(COMBINED + " $request_time")
        legacy = (FIXTURES / "combined.log").read_text().splitlines()[0]
        line = legacy + " 0.013"
        row = parse_line(line, compiled, "example.com", "secret")
        self.assertEqual(row.path, "/essays/hello")
        self.assertEqual(row.request_time_ms, 13)
        self.assertIsNone(
            parse_line(legacy, compiled, "example.com", "secret").request_time_ms
        )

    def test_host_format_accepts_timed_and_legacy_lines(self):
        legacy = (FIXTURES / "combined_host.log").read_text().strip()
        compiled = compile_log_format("combined_host")
        self.assertIsNone(
            parse_line(legacy, compiled, "example.com", "secret").request_time_ms
        )
        timed = parse_line(
            legacy + " 1.247", compiled, "example.com", "secret"
        )
        self.assertEqual(timed.request_time_ms, 1247)

    def test_self_referral_is_removed(self):
        line = (
            '203.0.113.10 - - [28/Sep/2026:10:15:00 -0500] "GET / HTTP/1.1" '
            '200 2 "https://www.example.com/other" "Mozilla/5.0 Safari/537.36"'
        )
        row = parse_line(line, compile_log_format("combined"), "example.com", "secret")
        self.assertIsNone(row.referrer_host)

    def test_bad_line_raises(self):
        with self.assertRaises(ParseError):
            parse_line("not a log line", compile_log_format("combined"), "example.com", "x")


if __name__ == "__main__":
    unittest.main()
