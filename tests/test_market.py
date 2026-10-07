"""'시장' 탭 시세: 네이버/야후 응답 해석, 종목별 실패 격리."""

import io
import json
import unittest

from laoer.market import Market, is_night, parse_naver, parse_yahoo

NAVER = {
    "closePrice": "1,055.20",
    "compareToPreviousClosePrice": "23.55",
    "compareToPreviousPrice": {"code": "5", "text": "하락", "name": "FALLING"},
    "fluctuationsRatio": "2.18",
    "localTradedAt": "2026-10-07T22:40:00+09:00",
}
YAHOO = {"chart": {"result": [{"meta": {"regularMarketPrice": 90.55, "previousClose": 89.44, "regularMarketTime": 1791379800}}]}}


class FakeOpener:
    def __init__(self, routes):
        self.routes, self.urls = routes, []

    def open(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        for key, body in self.routes.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                class R(io.BytesIO):
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                return R(json.dumps(body).encode())
        raise OSError("blocked")


class ParseTest(unittest.TestCase):
    def test_naver_falling_sign_and_time(self):
        q = parse_naver(NAVER)
        self.assertEqual((q["price"], q["change"], q["pct"]), (1055.2, -23.55, -2.18))
        self.assertEqual(q["at"], "2026-10-07T22:40:00+09:00")
        self.assertTrue(is_night(q["at"]))
        self.assertFalse(is_night("2026-10-07T15:45:00+09:00"))

    def test_naver_polling_shape(self):
        q = parse_naver({"datas": [{"closePrice": "1,078.75", "compareToPreviousClosePrice": "-24.25", "fluctuationsRatio": "-2.20"}]})
        self.assertEqual((q["price"], q["change"], q["pct"]), (1078.75, -24.25, -2.2))

    def test_yahoo_change_from_previous_close(self):
        q = parse_yahoo(YAHOO)
        self.assertEqual((q["price"], q["change"], q["pct"]), (90.55, 1.11, 1.24))
        self.assertTrue(q["at"].endswith("+09:00"))

    def test_day_close_is_labelled_as_such(self):
        day = {"closePrice": "1,078.75", "compareToPreviousClosePrice": "-24.25", "fluctuationsRatio": "-2.20",
               "marketStatus": "CLOSE", "localTradedAt": "2026-10-07T15:34:00+09:00"}
        rows = {r["id"]: r for r in Market(opener=FakeOpener({"polling.finance.naver.com": day})).snapshot()["rows"]}
        self.assertEqual(rows["k200f"]["name"], "코스피200 선물 (주간 종가)")
        self.assertNotIn("closed", rows["k200f"])

    def test_missing_price_raises(self):
        with self.assertRaises(ValueError):
            parse_naver({"closePrice": None})


class SnapshotTest(unittest.TestCase):
    def test_one_failure_does_not_hide_others_and_night_label(self):
        opener = FakeOpener({"polling.finance.naver.com": NAVER, "CL%3DF": YAHOO, "ES%3DF": OSError("down")})
        rows = {r["id"]: r for r in Market(opener=opener).snapshot()["rows"]}
        self.assertEqual(rows["k200f"]["name"], "코스피200 야간선물")  # 첫 주소 실패 → 두 번째 주소
        self.assertEqual(rows["wti"]["price"], 90.55)
        self.assertIn("down", rows["es"]["error"])
        self.assertIn("error", rows["nq"])


if __name__ == "__main__":
    unittest.main()
