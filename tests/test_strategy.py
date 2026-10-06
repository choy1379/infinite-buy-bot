import unittest
from decimal import Decimal as D

from laoer.strategy import Params, build_plan, star_pct, t_value

P = Params()  # 20분할, 목표 15%, 별% 15 - 1.5T


def legs(plan):
    return {o.leg: (o.side, o.tif, o.price, o.qty) for o in plan.orders}


class FormulaTest(unittest.TestCase):
    def test_defaults_match_v3_20split(self):
        self.assertEqual(P.base, D(15))
        self.assertEqual(P.slope, D("1.5"))
        self.assertEqual(star_pct(D(4), P), D(9))
        soxl = Params(target_pct=D(20))
        self.assertEqual(star_pct(D(3), soxl), D(14))  # 20 - 2T

    def test_t_value_rounds_up(self):
        self.assertEqual(t_value(D(7), D("71.43"), D(500)), D("1.01"))  # 500.01/500 = 1.00002 -> 1.01
        self.assertEqual(t_value(D(20), D(100), D(500)), D("4.00"))


class PlanTest(unittest.TestCase):
    def test_first_buy(self):
        plan = build_plan(qty=D(0), avg=D(0), last_price=D(100), unit=D(500), params=P)
        self.assertEqual(plan.phase, "first")
        self.assertEqual(legs(plan), {"first": ("BUY", "CLS", D("112.00"), 5)})

    def test_first_half(self):
        plan = build_plan(qty=D(20), avg=D(100), last_price=D(101), unit=D(500), params=P)
        self.assertEqual((plan.phase, plan.t, plan.star_pct, plan.star_price), ("first_half", D(4), D(9), D("109.00")))
        self.assertEqual(
            legs(plan),
            {
                "avg": ("BUY", "CLS", D("100.00"), 2),
                "star": ("BUY", "CLS", D("108.99"), 2),
                "quarter": ("SELL", "CLS", D("109.00"), 5),
                "target": ("SELL", "DAY", D("115.00"), 15),
            },
        )

    def test_second_half(self):
        plan = build_plan(qty=D(60), avg=D(100), last_price=D(95), unit=D(500), params=P)
        self.assertEqual((plan.phase, plan.t, plan.star_price), ("second_half", D(12), D("97.00")))
        self.assertEqual(
            legs(plan),
            {
                "star": ("BUY", "CLS", D("96.99"), 5),
                "quarter": ("SELL", "CLS", D("97.00"), 15),
                "target": ("SELL", "DAY", D("115.00"), 45),
            },
        )

    def test_exhausted_stops_buying(self):
        plan = build_plan(qty=D(95), avg=D(100), last_price=D(80), unit=D(500), params=P)
        self.assertEqual(plan.phase, "exhausted")
        self.assertEqual(plan.buys, [])
        self.assertEqual([o.leg for o in plan.sells], ["quarter", "target"])
        self.assertTrue(plan.notes)

    def test_buy_never_at_or_above_quarter_sell(self):
        # T가 9.99 근처면 별%가 0에 가까워 평단LOC와 쿼터LOC가 같아질 수 있다.
        plan = build_plan(qty=D(100), avg=D("49.95"), last_price=D(50), unit=D(500), params=P)
        sell = next(o for o in plan.orders if o.leg == "quarter")
        self.assertTrue(all(b.price < sell.price for b in plan.buys))

    def test_cash_cap_trims_expensive_leg_first(self):
        plan = build_plan(qty=D(20), avg=D(100), last_price=D(101), unit=D(500), params=P, cash=D(320))
        buys = {o.leg: o.qty for o in plan.buys}
        self.assertEqual(buys, {"avg": 2, "star": 1})
        self.assertTrue(any("매수가능금액" in n for n in plan.notes))

    def test_sellable_caps_sells(self):
        plan = build_plan(qty=D(20), avg=D(100), last_price=D(101), unit=D(500), params=P, sellable=D(8))
        self.assertEqual(sum(o.qty for o in plan.sells), 8)
        self.assertEqual(legs(plan)["quarter"][3], 2)

    def test_extra_levels_below_lowest_buy(self):
        p = Params(extra_buy_levels=3)
        plan = build_plan(qty=D(20), avg=D(100), last_price=D(101), unit=D(500), params=p)
        extras = [o for o in plan.orders if o.leg.startswith("extra")]
        # 본 매수 4주 → 500/5=100.00(평단LOC와 같아 제외), 500/6=83.33, 500/7=71.42
        self.assertEqual([(o.price, o.qty) for o in extras], [(D("83.33"), 1), (D("71.42"), 1)])

    def test_fractional_leftover_starts_new_cycle(self):
        plan = build_plan(qty=D("0.37"), avg=D(90), last_price=D(100), unit=D(500), params=P)
        self.assertEqual(plan.phase, "first")
        self.assertTrue(plan.notes)

    def test_unit_smaller_than_price(self):
        plan = build_plan(qty=D(0), avg=D(0), last_price=D(600), unit=D(500), params=P)
        self.assertEqual(plan.orders, [])


if __name__ == "__main__":
    unittest.main()
