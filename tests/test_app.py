import unittest
from datetime import date, datetime
from unittest.mock import patch

import app


class TideAppTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_find_high_low_detects_turning_points(self):
        day = date(2026, 5, 14)
        points = [
            app.TidePoint(datetime.fromisoformat(f"{day}T00:00:00+02:00"), 10.0, "meting"),
            app.TidePoint(datetime.fromisoformat(f"{day}T01:00:00+02:00"), 30.0, "meting"),
            app.TidePoint(datetime.fromisoformat(f"{day}T02:00:00+02:00"), 5.0, "meting"),
            app.TidePoint(datetime.fromisoformat(f"{day}T07:00:00+02:00"), 40.0, "meting"),
            app.TidePoint(datetime.fromisoformat(f"{day}T12:00:00+02:00"), 8.0, "meting"),
            app.TidePoint(datetime.fromisoformat(f"{day}T18:00:00+02:00"), 35.0, "meting"),
            app.TidePoint(datetime.fromisoformat(f"{day}T22:00:00+02:00"), 9.0, "meting"),
        ]

        highs, lows = app._find_high_low(points)

        self.assertGreaterEqual(len(highs), 2)
        self.assertGreaterEqual(len(lows), 2)

    @patch("app._find_high_low")
    @patch("app._merge_points")
    def test_api_tides_returns_json(self, merge_mock, highlow_mock):
        point = app.TidePoint(datetime.fromisoformat("2026-05-14T06:00:00+02:00"), 123.4, "verwachting")
        merge_mock.return_value = [point]
        highlow_mock.return_value = ([point], [point])

        response = self.client.get("/api/tides?date=2026-05-14")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["date"], "2026-05-14")
        self.assertEqual(payload["point_count"], 1)
        self.assertEqual(payload["high_waters"][0]["value_cm"], 123.4)


if __name__ == "__main__":
    unittest.main()

