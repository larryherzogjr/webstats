import unittest

from webstats.bots import classify_feed_reader, classify_user_agent


class BotTests(unittest.TestCase):
    def test_known_crawlers(self):
        for value in (
            "Googlebot/2.1",
            "GPTBot/1.0",
            "ClaudeBot/1.0",
            "Mozilla/5.0; compatible; Claude-SearchBot/1.0",
            "Perplexity-User/1.0",
            "CCBot/2.0",
        ):
            with self.subTest(value=value):
                self.assertTrue(classify_user_agent(value)[1])

    def test_generic_clients(self):
        for value in ("", "curl/8.0", "python-requests/2.31", "CustomClient/1"):
            with self.subTest(value=value):
                self.assertTrue(classify_user_agent(value)[1])

    def test_browser_is_human(self):
        family, is_bot = classify_user_agent(
            "Mozilla/5.0 AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
        )
        self.assertEqual(family, "Chrome")
        self.assertFalse(is_bot)

    def test_feed_readers_and_reported_subscriber_counts(self):
        cases = (
            (
                "Inoreader/1.0 (+http://www.inoreader.com/feed-fetcher; 3 subscribers; )",
                ("Inoreader", 3),
            ),
            (
                "NewsBlur Feed Fetcher - 1,234 subscribers - https://newsblur.com",
                ("NewsBlur", 1234),
            ),
            ("Feedly/1.0 (+https://feedly.com/fetcher.html)", ("Feedly", None)),
            (
                "Feedspot/1.0 (+https://www.feedspot.com/fs/fetcher; like FeedFetcher-Google)",
                ("Feedspot", None),
            ),
            ("FreshRSS/1.24.3", ("FreshRSS", None)),
            ("Miniflux/2.2.0", ("Miniflux", None)),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(classify_feed_reader(value), expected)
                self.assertEqual(classify_user_agent(value), (expected[0], True))

        self.assertEqual(
            classify_feed_reader("NewsBlur Feed Fetcher - -1 subscribers"),
            ("NewsBlur", None),
        )
        self.assertIsNone(classify_feed_reader("Mozilla/5.0 Chrome/128.0"))


if __name__ == "__main__":
    unittest.main()
