"""'시장' 탭 시세: 네이버/야후 응답 해석, 종목별 실패 격리."""

import io
import json
import unittest

from datetime import datetime, timezone

from laoer.market import Market, is_night, parse_naver, parse_prober, parse_yahoo

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


DAY = {"closePrice": "1,078.75", "marketStatus": "CLOSE", "localTradedAt": "2026-10-07T15:34:00+09:00"}


def bars(*closes, end="2026-10-07T23:07:00+09:00"):
    t = int(datetime.fromisoformat(end).timestamp())
    n = len(closes)
    return {"success": True, "data": [{"date": t - 60 * (n - 1 - i), "close": c} for i, c in enumerate(closes)]}


def at_kst(iso):
    return lambda: datetime.fromisoformat(iso).astimezone(timezone.utc)


class NightTest(unittest.TestCase):
    def market(self, routes, now="2026-10-07T23:08:00+09:00"):
        self.opener = FakeOpener({"polling.finance.naver.com": DAY, **routes})
        return Market(opener=self.opener, now=at_kst(now))

    def k200(self, m):
        return {r["id"]: r for r in m.snapshot()["rows"]}["k200f"]

    def test_parse_prober_uses_last_close_and_day_close_as_base(self):
        q = parse_prober(bars(1070.0, 1061.9), 1078.75)
        self.assertEqual((q["price"], q["change"], q["pct"]), (1061.9, -16.85, -1.56))
        self.assertEqual(q["at"], "2026-10-07T23:07:00+09:00")

    def test_night_quote_replaces_day_close(self):
        row = self.k200(self.market({"api.prober.kr": bars(1070.0, 1061.9)}))
        self.assertEqual((row["name"], row["price"], row["change"]), ("코스피200 야간선물", 1061.9, -16.85))
        self.assertTrue(any("api.prober.kr" in u for u in self.opener.urls))

    def test_prober_failure_falls_back_to_day_close(self):
        row = self.k200(self.market({"api.prober.kr": OSError("closed")}))
        self.assertEqual((row["name"], row["price"]), ("코스피200 선물 (주간 종가)", 1078.75))

    def test_stale_night_bars_are_not_used(self):
        row = self.k200(self.market({"api.prober.kr": bars(1061.9, end="2026-10-07T20:00:00+09:00")}))
        self.assertEqual(row["name"], "코스피200 선물 (주간 종가)")

    def test_daytime_never_calls_prober(self):
        m = self.market({"api.prober.kr": bars(1061.9)}, now="2026-10-07T11:00:00+09:00")
        self.k200(m)
        self.assertFalse(any("api.prober.kr" in u for u in self.opener.urls))

    def test_prober_response_is_cached(self):
        m = self.market({"api.prober.kr": bars(1061.9)})
        m.snapshot()
        m.snapshot()
        self.assertEqual(sum("api.prober.kr" in u for u in self.opener.urls), 1)


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
