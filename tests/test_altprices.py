import unittest

from laoer import altprices


class ParseTest(unittest.TestCase):
    def test_new_york_dates_and_skips_empty(self):
        chart = {"chart": {"result": [{
            "timestamp": [1760535000, 1760621400, 1760707800],  # 10/15, 10/16, 10/17 09:30 뉴욕
            "indicators": {"quote": [{"close": [110.123456, None, 98.5]}]},
        }]}}
        self.assertEqual(altprices.parse(chart), {"2025-10-15": 110.1235, "2025-10-17": 98.5})


if __name__ == "__main__":
    unittest.main()
