from datetime import date, timedelta
import unittest

from webstats.episodes import detect_episodes


class EpisodeDetectionTests(unittest.TestCase):
    def test_detects_peak_decay_and_return_to_baseline(self):
        first = date(2026, 8, 23)
        values = [2] * 28 + [12, 8, 3, 2] + [2] * 7
        series = [
            {"day": (first + timedelta(days=index)).isoformat(), "requests": value}
            for index, value in enumerate(values)
        ]
        episodes = detect_episodes(
            series, date(2026, 9, 20), date(2026, 9, 21)
        )
        self.assertEqual(len(episodes), 1)
        episode = episodes[0]
        self.assertEqual(episode["start"], "2026-09-20")
        self.assertEqual(episode["end"], "2026-09-22")
        self.assertEqual(episode["peak_requests"], 12)
        self.assertEqual(episode["peak_multiple"], 6.0)
        self.assertEqual(episode["lasting_effect"], "returned")
        self.assertEqual(episode["trajectory"], "recovered")

    def test_sustained_aftermath_and_ordinary_noise(self):
        first = date(2026, 1, 1)
        values = [1] * 28 + [10] + [4] * 7 + [3] * 7
        series = [
            {"day": (first + timedelta(days=index)).isoformat(), "requests": value}
            for index, value in enumerate(values)
        ]
        episodes = detect_episodes(
            series, first + timedelta(days=28), first + timedelta(days=28)
        )
        self.assertEqual(episodes[0]["lasting_effect"], "sustained")

        quiet = [
            {"day": (first + timedelta(days=index)).isoformat(), "requests": index % 2}
            for index in range(40)
        ]
        self.assertEqual(
            detect_episodes(quiet, first + timedelta(days=28), first + timedelta(days=39)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
