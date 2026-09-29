from datetime import date, timedelta
import unittest

from webstats.reliability import detect_error_bursts


class ReliabilityDetectionTests(unittest.TestCase):
    def test_error_burst_tracks_peak_and_recovery(self):
        first = date(2026, 8, 23)
        values = [0] * 28 + [7, 4, 1, 0] + [0] * 4
        series = [
            {
                "day": (first + timedelta(days=index)).isoformat(),
                "app_errors": value,
                "server_errors": 1 if index == 28 else 0,
            }
            for index, value in enumerate(values)
        ]
        incidents = detect_error_bursts(
            series, date(2026, 9, 20), date(2026, 9, 21)
        )
        self.assertEqual(len(incidents), 1)
        incident = incidents[0]
        self.assertEqual(incident["peak_errors"], 7)
        self.assertEqual(incident["errors"], 11)
        self.assertEqual(incident["server_errors"], 1)
        self.assertTrue(incident["recovered"])
        self.assertEqual(incident["recovery_day"], "2026-09-22")

    def test_ordinary_errors_do_not_form_burst(self):
        first = date(2026, 1, 1)
        series = [
            {
                "day": (first + timedelta(days=index)).isoformat(),
                "app_errors": index % 2,
                "server_errors": 0,
            }
            for index in range(40)
        ]
        self.assertEqual(
            detect_error_bursts(
                series, first + timedelta(days=28), first + timedelta(days=39)
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
