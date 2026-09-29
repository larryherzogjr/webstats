import unittest

from webstats.robots import parse_robots, path_allowed, policy_for


class RobotsTests(unittest.TestCase):
    def test_explicit_agent_beats_wildcard_and_allow_wins_tie(self):
        groups = parse_robots("""
User-agent: *
Disallow: /shared

User-agent: GPTBot
Disallow: /private
Allow: /private/public
""")
        source, rules = policy_for(groups, "GPTBot")
        self.assertEqual(source, "explicit")
        self.assertFalse(path_allowed(rules, "/private/story"))
        self.assertTrue(path_allowed(rules, "/private/public/story"))
        self.assertTrue(path_allowed(rules, "/shared/story"))

    def test_wildcards_terminal_matches_and_empty_disallow(self):
        groups = parse_robots("""
User-agent: *
Disallow:
Disallow: /*.pdf$
Allow: /public/*.pdf$
""")
        source, rules = policy_for(groups, "ClaudeBot")
        self.assertEqual(source, "wildcard")
        self.assertFalse(path_allowed(rules, "/docs/file.pdf"))
        self.assertTrue(path_allowed(rules, "/docs/file.pdf?download=1"))
        self.assertTrue(path_allowed(rules, "/public/file.pdf"))


if __name__ == "__main__":
    unittest.main()
