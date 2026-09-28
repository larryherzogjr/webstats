import unittest

from webstats.bots import classify_user_agent


class BotTests(unittest.TestCase):
    def test_known_crawlers(self):
        for value in ("Googlebot/2.1", "GPTBot/1.0", "ClaudeBot/1.0", "CCBot/2.0"):
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


if __name__ == "__main__":
    unittest.main()

