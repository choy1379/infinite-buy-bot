"""봇 밖에서 판 종목의 실현 손익 기록: 계산, 중복 방지, 평단 보존."""

import tempfile
import unittest
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

from laoer.realized import KST, RealizedLog, build_record, normalize_sell, sync, sync_view

SELL_KO = {
    "orderId": "k1", "symbol": "KO", "side": "SELL", "status": "FILLED", "currency": "USD", "quantity": "120", "price": "87.00",
    "orderedAt": "2026-10-08T22:50:00+09:00",
    "execution": {"filledQuantity": "120", "averageFilledPrice": "87.00", "filledAmount": "10440.00", "commission": "1.04", "tax": "0.30", "filledAt": "2026-10-08T23:40:12+09:00"},
}
BUY_KO = {**SELL_KO, "orderId": "k0", "side": "BUY"}
CANCELED = {**SELL_KO, "orderId": "k2", "status": "CANCELED", "execution": {"filledQuantity": "0"}}
SELL_TECL = {**SELL_KO, "orderId": "t1", "symbol": "TECL"}
HOLD = {"items": [{"symbol": "KO", "name": "코카콜라", "quantity": "314", "averagePurchasePrice": "87.744493"}]}


class FakeToss:
    def __init__(self, orders, held=HOLD):
        self.orders, self.held, self.fx_at, self.fail_orders = orders, held, [], False

    def holdings_all(self):
        return self.held

    def closed_orders(self, **kw):
        if self.fail_orders:
            raise RuntimeError("down")
        return self.orders

    def exchange_rate(self, at=None, **kw):
        self.fx_at.append(at)
        return D("1388.50")


class NormalizeTest(unittest.TestCase):
    def test_only_filled_sells(self):
        self.assertEqual(normalize_sell(SELL_KO)["qty"], D(120))
        self.assertIsNone(normalize_sell(BUY_KO))
        self.assertIsNone(normalize_sell(CANCELED))

    def test_loss_in_usd_and_krw(self):
        rec = build_record(normalize_sell(SELL_KO), D("87.744493"), D("1388.50"), "코카콜라")
        # (87.00 - 87.744493) × 120 - 1.04 - 0.30 = -90.67916
        self.assertEqual(rec["plUsd"], "-90.68")
        self.assertEqual(rec["plKrw"], "-125908")  # -90.67916 × 1388.50
        self.assertEqual((rec["date"], rec["qty"], rec["price"], rec["fx"]), ("2026-10-08", "120", "87.00", "1388.50"))

    def test_unknown_cost_leaves_pl_empty(self):
        rec = build_record(normalize_sell(SELL_KO), None, D("1388.50"))
        self.assertIsNone(rec["plUsd"])
        self.assertIsNone(rec["plKrw"])

    def test_krw_stock_is_won_only(self):
        sell = {**SELL_KO, "symbol": "005930", "currency": "KRW"}
        sell["execution"] = {**SELL_KO["execution"], "averageFilledPrice": "58900", "filledQuantity": "10", "commission": "0", "tax": "0"}
        rec = build_record(normalize_sell(sell), D("61200"), None)
        self.assertIsNone(rec["plUsd"])
        self.assertEqual(rec["plKrw"], "-23000")


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "realized.json"
        self.now = datetime(2026, 10, 9, 1, 0, tzinfo=KST)

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_new_sells_once_and_skips_bot_symbol(self):
        toss = FakeToss([SELL_KO, BUY_KO, CANCELED, SELL_TECL])
        rlog = RealizedLog(self.path)
        new = sync(toss, rlog, skip_symbol="TECL", now=self.now)
        self.assertEqual([r["orderId"] for r in new], ["k1"])
        self.assertEqual(new[0]["name"], "코카콜라")
        self.assertEqual(toss.fx_at, ["2026-10-08T23:40:12+09:00"])  # 체결 시각의 환율
        again = sync(toss, RealizedLog(self.path), skip_symbol="TECL", now=self.now)
        self.assertEqual(again, [])  # 다시 돌려도 중복 기록 없음
        view = RealizedLog(self.path).view()
        self.assertEqual((view["totalUsd"], view["totalKrw"], len(view["items"])), ("-90.68", "-125908", 1))

    def test_cost_is_kept_after_everything_is_sold(self):
        sync(FakeToss([], HOLD), RealizedLog(self.path), skip_symbol="TECL", now=self.now)
        later = FakeToss([SELL_KO], {"items": []})  # 다 팔아서 보유에서 사라진 뒤
        new = sync(later, RealizedLog(self.path), skip_symbol="TECL", now=self.now)
        self.assertEqual(new[0]["cost"], "87.744493")

    def test_sync_view_survives_api_failure_and_keeps_old_records(self):
        bot = SimpleNamespace(cfg=SimpleNamespace(run=SimpleNamespace(state_dir=Path(self.tmp.name))), toss=FakeToss([SELL_KO]), symbol="TECL")
        v = sync_view(bot)
        self.assertEqual(len(v["items"]), 1)
        bot.toss.fail_orders = True
        self.assertEqual(len(sync_view(bot)["items"]), 1)

    def test_empty_gives_none(self):
        bot = SimpleNamespace(cfg=SimpleNamespace(run=SimpleNamespace(state_dir=Path(self.tmp.name))), toss=FakeToss([]), symbol="TECL")
        self.assertIsNone(sync_view(bot))


if __name__ == "__main__":
    unittest.main()
