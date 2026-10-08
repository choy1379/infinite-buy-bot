"""올해 양도세 대략치: 이동평균 원화 손익, 공제·세율, 평단 모르는 매도 제외."""

import tempfile
import unittest
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

from laoer.realized import KST
from laoer.tax import TaxLog, compute, refresh, year_orders


def order(oid, sym, side, qty, px, at, fee="0", tax="0"):
    return {
        "orderId": oid, "symbol": sym, "side": side, "currency": "USD", "orderedAt": at,
        "execution": {"filledQuantity": str(qty), "averageFilledPrice": str(px), "commission": fee, "tax": tax, "filledAt": at},
    }


FX = {"2026-03-01": D("1300"), "2026-06-01": D("1400")}


def fx_of(iso):
    return FX[iso[:10]]


class ComputeTest(unittest.TestCase):
    def test_gain_uses_buy_and_sell_day_fx(self):
        orders = [order("b", "AAA", "BUY", 10, 100, "2026-03-01T10:00:00+09:00"), order("s", "AAA", "SELL", 10, 110, "2026-06-01T10:00:00+09:00")]
        r = compute(orders, fx_of)
        # 매도 10*110*1400 - 매수 10*100*1300 = 1,540,000 - 1,300,000
        self.assertEqual(r["gainKrw"], "240000")
        self.assertEqual((r["taxableKrw"], r["taxKrw"]), ("0", "0"))  # 기본공제 250만원 아래

    def test_deduction_and_rate(self):
        orders = [order("b", "AAA", "BUY", 100, 100, "2026-03-01T10:00:00+09:00"), order("s", "AAA", "SELL", 100, 120, "2026-06-01T10:00:00+09:00")]
        r = compute(orders, fx_of)
        self.assertEqual(r["gainKrw"], str(100 * 120 * 1400 - 100 * 100 * 1300))  # 3,800,000
        self.assertEqual(r["taxableKrw"], "1300000")
        self.assertEqual(r["taxKrw"], "286000")  # 22%

    def test_first_in_first_out_losses_and_fees(self):
        orders = [
            order("b1", "AAA", "BUY", 10, 100, "2026-03-01T10:00:00+09:00"),
            order("b2", "AAA", "BUY", 10, 120, "2026-03-01T11:00:00+09:00"),
            order("s", "AAA", "SELL", 5, 90, "2026-06-01T10:00:00+09:00", fee="1"),
        ]
        r = compute(orders, fx_of)
        # 먼저 산 100$ 5주를 판 것으로 계산: (5*90-1)*1400 - 5*100*1300 = 628,600 - 650,000
        self.assertEqual(r["gainKrw"], "-21400")
        self.assertEqual(r["bySymbol"], {"AAA": "-21400"})

    def test_sell_spanning_two_lots(self):
        orders = [
            order("b1", "AAA", "BUY", 3, 100, "2026-03-01T10:00:00+09:00"),
            order("b2", "AAA", "BUY", 3, 200, "2026-03-01T11:00:00+09:00"),
            order("s1", "AAA", "SELL", 4, 150, "2026-06-01T10:00:00+09:00"),
            order("s2", "AAA", "SELL", 2, 150, "2026-06-01T11:00:00+09:00"),
        ]
        r = compute(orders, fx_of)
        # s1: 3주(100$)+1주(200$), s2: 남은 2주(200$)
        self.assertEqual(r["gainKrw"], str(6 * 150 * 1400 - (3 * 100 + 3 * 200) * 1300))  # 1,260,000 - 1,170,000
        self.assertEqual(r["unknown"], {})

    def test_sell_without_known_buy_is_left_out_and_counted(self):
        orders = [order("s", "OLD", "SELL", 5, 90, "2026-06-01T10:00:00+09:00")]
        r = compute(orders, fx_of)
        self.assertEqual((r["gainKrw"], r["unknown"]), ("0", {"OLD": 1}))

    def test_krw_stocks_are_ignored(self):
        o = order("k", "005930", "BUY", 1, 70000, "2026-03-01T10:00:00+09:00")
        o["currency"] = "KRW"
        self.assertEqual(compute([o], fx_of)["gainKrw"], "0")


class FakeToss:
    def __init__(self, orders):
        self.orders, self.fx_calls, self.windows = orders, [], []

    def closed_orders(self, **kw):
        self.windows.append((kw["start"], kw["end"]))
        return [o for o in self.orders if kw["start"] <= o["orderedAt"][:10] <= kw["end"]]

    def exchange_rate(self, at=None, **kw):
        self.fx_calls.append(at)
        return FX[at[:10]]


class RefreshTest(unittest.TestCase):
    def test_windows_cover_the_year_and_fx_is_cached_per_day(self):
        orders = [order("b", "AAA", "BUY", 10, 100, "2026-03-01T10:00:00+09:00"), order("b2", "AAA", "BUY", 1, 100, "2026-03-01T12:00:00+09:00"),
                  order("s", "AAA", "SELL", 11, 110, "2026-06-01T10:00:00+09:00")]
        toss = FakeToss(orders)
        now = datetime(2026, 10, 9, 1, 0, tzinfo=KST)
        with tempfile.TemporaryDirectory() as d:
            tlog = TaxLog(Path(d) / "tax.json")
            r = refresh(toss, tlog, now)
            self.assertEqual(len(toss.fx_calls), 2)  # 3/1 은 한 번만
            self.assertEqual(r["gainKrw"], str(11 * 110 * 1400 - 11 * 100 * 1300))
            self.assertEqual(min(w[0] for w in toss.windows), "2026-01-01")
            self.assertEqual(max(w[1] for w in toss.windows), "2026-10-09")
            again = TaxLog(Path(d) / "tax.json")
            self.assertFalse(again.stale(now))
            self.assertTrue(again.stale(datetime(2026, 10, 9, 8, 0, tzinfo=KST)))  # 6시간 뒤엔 다시 계산


if __name__ == "__main__":
    unittest.main()
