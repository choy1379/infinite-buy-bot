"""가짜 브로커로 하루하루 장을 돌려 봇 흐름(주문 → 체결 → 리포트 → 사이클 완료)을 검증한다."""

import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from laoer.bot import KST, Bot
from laoer.config import load_config
from laoer.notify import Notifier
from laoer.state import State
from laoer.toss import Holding, TossError


def session_dict(date: str) -> dict:
    y, m, d = map(int, date.split("-"))
    start = datetime(y, m, d, 22, 30, tzinfo=KST)
    return {
        "date": date,
        "regularMarket": {"startTime": start.isoformat(), "endTime": (start + timedelta(hours=6, minutes=30)).isoformat()},
    }


class FakeBroker:
    account_seq = 1

    def __init__(self, clock, dates):
        self.clock = clock
        self.dates = dates
        self.last_price = D(100)
        self.qty = D(0)
        self.avg = D(0)
        self.cash = D(10000)
        self.orders: dict[str, dict] = {}
        self.manual_open: list[dict] = []
        self.fail_orders = False

    # --- 봇이 쓰는 인터페이스
    def market_calendar_us(self):
        sessions = [session_dict(d) for d in self.dates]
        now = self.clock()
        i = next(
            (k for k, s in enumerate(sessions) if datetime.fromisoformat(s["regularMarket"]["endTime"]) > now),
            len(sessions) - 1,
        )
        pick = lambda k: sessions[k] if 0 <= k < len(sessions) else {"date": "2099-01-01"}
        return {"previousBusinessDay": pick(i - 1), "today": pick(i), "nextBusinessDay": pick(i + 1)}

    def price(self, symbol):
        return self.last_price

    def holding(self, symbol):
        if self.qty <= 0:
            return None
        rate = self.last_price / self.avg - 1
        return Holding(symbol, self.qty, self.avg, self.last_price, self.qty * self.avg, rate)

    def buying_power(self, currency="USD"):
        return self.cash

    def sellable_quantity(self, symbol):
        return self.qty

    def open_orders(self, symbol=None):
        return self.manual_open + [o for o in self.orders.values() if o["status"] == "PENDING"]

    def place_order(self, *, symbol, side, tif, qty, price, client_order_id):
        if self.fail_orders:
            raise TossError(422, "market-closed", "주문 불가 시간")
        oid = f"oid-{client_order_id}"
        self.orders[oid] = {
            "orderId": oid, "cid": client_order_id, "side": side, "tif": tif, "qty": qty, "price": price,
            "status": "PENDING", "fill": (0, None),
        }
        return oid

    def get_order(self, order_id):
        o = self.orders[order_id]
        q, p = o["fill"]
        return {
            "status": o["status"],
            "execution": {
                "filledQuantity": str(q),
                "averageFilledPrice": None if p is None else str(p),
                "filledAmount": str(q * (p or 0)),
                "commission": "0",
                "tax": None,
            },
        }

    def cancel_order(self, order_id):
        self.orders[order_id]["status"] = "CANCELED"
        return order_id + "-c"

    # --- 시뮬레이션
    def close_day(self, close, high=None):
        high = high or close
        for o in self.orders.values():
            if o["status"] != "PENDING":
                continue
            price, fill = o["price"], None
            if o["tif"] == "CLS":
                if (o["side"] == "BUY" and close <= price) or (o["side"] == "SELL" and close >= price):
                    fill = close
            elif o["side"] == "SELL" and high >= price:
                fill = price
            if fill is None:
                o["status"] = "CANCELED"
                continue
            q = o["qty"]
            if o["side"] == "BUY":
                self.avg = (self.qty * self.avg + q * fill) / (self.qty + q)
                self.qty += q
            else:
                self.qty -= q
                if self.qty == 0:
                    self.avg = D(0)
            o["status"], o["fill"] = "FILLED", (q, fill)
        self.last_price = close


class Capture:
    name = "capture"

    def __init__(self):
        self.messages = []

    def send(self, msg):
        self.messages.append(msg)

    def titles(self):
        return [m.title for m in self.messages]


CONFIG = """
[toss]
client_id = "c"
client_secret = "s"
[strategy]
symbol = "TECL"
capital_usd = 10000
splits = 20
target_pct = 15
[run]
mode = "{mode}"
dry_run = {dry_run}
report_not_before_kst = "{not_before}"
state_dir = "state"
"""


class BotFlowTest(unittest.TestCase):
    DATES = ["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [datetime(2026, 10, 6, 20, 0, tzinfo=KST)]
        self.broker = FakeBroker(lambda: self.now[0], self.DATES)
        self.capture = Capture()

    def tearDown(self):
        self.tmp.cleanup()

    def make_bot(self, dry_run=False, not_before="", mode="trade"):
        path = Path(self.tmp.name) / "config.toml"
        path.write_text(CONFIG.format(mode=mode, dry_run=str(dry_run).lower(), not_before=not_before), encoding="utf-8")
        cfg = load_config(path)
        state = State(cfg.run.state_dir / "state.json")
        return Bot(cfg, self.broker, Notifier([self.capture]), state, clock=lambda: self.now[0], sleep=lambda s: None)

    def at(self, y, m, d, hh, mm):
        self.now[0] = datetime(y, m, d, hh, mm, tzinfo=KST)

    def test_full_cycle(self):
        bot = self.make_bot()
        # 장 시작 전: 기다리기만 한다
        wait = bot.tick()
        self.assertAlmostEqual(wait, 2 * 3600 + 45 * 60)
        self.assertEqual(self.broker.orders, {})

        # 1일차 22:45 — 첫 매수
        self.at(2026, 10, 6, 22, 45)
        bot.tick()
        (first,) = self.broker.orders.values()
        self.assertEqual((first["cid"], first["side"], first["tif"], first["qty"], first["price"]),
                         ("lab-TECL-20261006-first", "BUY", "CLS", 5, D("112.00")))
        self.assertIn("첫 매수", self.capture.titles()[-1])
        bot.tick()  # 같은 날 다시 돌아도 중복 주문 없음
        self.assertEqual(len(self.broker.orders), 1)

        self.broker.close_day(D(100))
        self.at(2026, 10, 7, 5, 20)
        bot.tick()
        report = self.capture.messages[-1]
        self.assertIn("체결 결과", report.title)
        self.assertIn("✅ 매수 첫매수LOC 5/5주 @ $100.00", report.lines)
        self.assertEqual(bot.state.cycle["bought"], "500.00")
        self.assertEqual((bot.state.days["2026-10-06"]["close"], bot.state.days["2026-10-06"]["daily_pct"]), ("100", "0.00"))

        # 2일차 — 전반전: T=1, 별% 13.5
        self.at(2026, 10, 7, 22, 45)
        bot.tick()
        day2 = {o["cid"].split("-")[-1]: (o["side"], o["tif"], o["qty"], o["price"])
                for o in self.broker.orders.values() if "20261007" in o["cid"]}
        self.assertEqual(day2, {
            "avg": ("BUY", "CLS", 2, D("100.00")),
            "star": ("BUY", "CLS", 2, D("113.49")),
            "quarter": ("SELL", "CLS", 1, D("113.50")),
            "target": ("SELL", "DAY", 4, D("115.00")),
        })
        self.assertIn("전반전 T1.00", self.capture.titles()[-1])

        # 장중 +16% 찍고 114 마감 → 목표 4주 + 쿼터 1주 모두 매도 → 사이클 완료
        self.broker.close_day(D(114), high=D(116))
        self.at(2026, 10, 8, 5, 20)
        bot.tick()
        titles = self.capture.titles()
        self.assertIn("사이클 #1 완료", titles[-1])
        self.assertIn("실현손익 $74.00 (원금 대비 +0.74%)", self.capture.messages[-1].lines)
        self.assertIsNone(bot.state.cycle)
        # 일간: 전일 종가 100 x 5주 = 500 → 다 팔아 574 → +74 (+14.80%)
        d2 = bot.state.days["2026-10-07"]
        self.assertEqual((d2["close"], d2["pnl"], d2["daily_pct"]), ("114", "74.00", "14.80"))

        # 3일차 — 새 사이클 첫 매수
        self.at(2026, 10, 8, 22, 45)
        bot.tick()
        self.assertEqual(bot.state.cycle["id"], 2)
        self.assertIn("lab-TECL-20261008-first", [o["cid"] for o in self.broker.orders.values()])

    def test_dry_run_places_nothing(self):
        bot = self.make_bot(dry_run=True)
        self.at(2026, 10, 6, 22, 45)
        bot.tick()
        self.assertEqual(self.broker.orders, {})
        self.assertTrue(self.capture.titles()[-1].startswith("🧪 [모의]"))
        self.at(2026, 10, 7, 5, 30)
        bot.tick()
        self.assertIn("(모의)", self.capture.titles()[-1])

    def test_blocks_on_manual_open_orders(self):
        self.broker.manual_open = [{"orderId": "manual"}]
        bot = self.make_bot()
        self.at(2026, 10, 6, 22, 45)
        bot.tick()
        bot.tick()
        self.assertEqual(self.broker.orders, {})
        self.assertEqual(bot.state.days["2026-10-06"]["status"], "blocked")
        self.assertEqual(sum("건너뜀" in t for t in self.capture.titles()), 1)

    def test_missed_when_started_after_cutoff(self):
        bot = self.make_bot()
        self.at(2026, 10, 7, 4, 50)
        bot.tick()
        self.assertEqual(self.broker.orders, {})
        self.assertEqual(bot.state.days["2026-10-06"]["status"], "missed")

    def test_adopts_existing_position(self):
        self.broker.qty, self.broker.avg = D(20), D(100)
        bot = self.make_bot()
        self.at(2026, 10, 6, 22, 45)
        bot.tick()
        cycle = bot.state.cycle
        self.assertEqual((cycle["adopted"], cycle["bought"]), (True, "2000.00"))
        self.assertTrue(any("이어받아" in line for line in self.capture.messages[-1].lines))
        self.assertEqual(len(self.broker.orders), 4)

    def test_failed_orders_are_retried_with_new_ids(self):
        self.broker.fail_orders = True
        bot = self.make_bot()
        self.at(2026, 10, 6, 22, 45)
        self.assertEqual(bot.tick(), 600)
        self.assertEqual(bot.state.days["2026-10-06"]["status"], "failed")
        self.assertEqual(self.capture.messages[-1].level, "error")
        self.broker.fail_orders = False
        self.at(2026, 10, 6, 22, 55)
        bot.tick()
        self.assertEqual([o["cid"] for o in self.broker.orders.values()], ["lab-TECL-20261006-first-r2"])
        self.assertEqual(bot.state.days["2026-10-06"]["status"], "placed")

    def test_report_waits_for_not_before(self):
        bot = self.make_bot(not_before="07:30")
        self.at(2026, 10, 6, 22, 45)
        bot.tick()
        self.broker.close_day(D(100))
        self.at(2026, 10, 7, 5, 30)
        bot.tick()
        self.assertNotIn("체결 결과", self.capture.titles()[-1])
        self.at(2026, 10, 7, 7, 31)
        bot.tick()
        self.assertIn("체결 결과", self.capture.titles()[-1])

    def test_preview_does_not_touch_state(self):
        bot = self.make_bot()
        msg = bot.preview()
        self.assertTrue(msg.title.startswith("🔎 [미리보기]"))
        self.assertIsNone(bot.state.cycle)
        self.assertEqual(bot.state.days, {})
        self.assertEqual(self.broker.orders, {})

    def test_force_replace_cancels_previous_orders(self):
        self.broker.qty, self.broker.avg = D(20), D(100)
        bot = self.make_bot()
        self.at(2026, 10, 6, 22, 45)
        bot.tick()
        first_ids = list(self.broker.orders)
        s = bot.next_session()
        bot.place(s, force=True)
        self.assertTrue(all(self.broker.orders[i]["status"] == "CANCELED" for i in first_ids))
        pending = [o for o in self.broker.orders.values() if o["status"] == "PENDING"]
        self.assertEqual(len(pending), 4)
        self.assertTrue(all(o["cid"].endswith("-r2") for o in pending))


    def test_alert_mode_never_orders_and_reports_holdings_change(self):
        self.broker.manual_open = [{"orderId": "manual"}]  # 직접 넣은 주문이 있어도 정상
        bot = self.make_bot(mode="alert")
        self.at(2026, 10, 6, 22, 45)
        bot.tick()
        self.assertEqual(self.broker.orders, {})
        msg = self.capture.messages[-1]
        self.assertTrue(msg.title.startswith("🔔 TECL 오늘 넣을 주문"))
        self.assertIn("매수 첫매수LOC 5주 @ $112.00", msg.lines)
        self.assertTrue(any("직접 넣으세요" in line for line in msg.lines))

        # 사용자가 앱에서 직접 산 결과
        self.broker.qty, self.broker.avg, self.broker.last_price = D(5), D(100), D(100)
        self.at(2026, 10, 7, 5, 20)
        bot.tick()
        report = self.capture.messages[-1]
        self.assertIn("장 마감 결과", report.title)
        self.assertIn("보유 0 → 5주 (+5주)", report.lines)
        self.assertEqual(bot.state.cycle["bought"], "500.00")

        # 다음 날 전량 매도 → 사이클 완료
        self.at(2026, 10, 7, 22, 45)
        bot.tick()
        self.assertIn("오늘 넣을 주문", self.capture.titles()[-1])
        self.broker.qty, self.broker.avg = D(0), D(0)
        self.at(2026, 10, 8, 5, 20)
        bot.tick()
        self.assertIn("사이클 #1 완료", self.capture.titles()[-1])
        self.assertTrue(any("토스증권 앱" in line for line in self.capture.messages[-1].lines))
        self.assertEqual(self.broker.orders, {})


if __name__ == "__main__":
    unittest.main()
