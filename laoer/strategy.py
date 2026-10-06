"""무한매수법 주문 계산 (순수 함수, 네트워크 없음).

V3.0 20분할 기준 규칙:
  - 1회 매수금 = 원금 / 분할수
  - T = (보유수량 x 평단) / 1회 매수금, 소수 둘째 자리 올림
  - 별% = star_base - star_slope x T   (기본: 목표% x (1 - 2T/분할수), 20분할·15%면 15 - 1.5T)
  - 별지점 = 평단 x (1 + 별%)
  - 첫 매수(보유 0주): 1회 매수금 전부, 현재가 +premium% LOC (사실상 종가 매수)
  - 전반전(T < 분할수/2): 절반 평단 LOC, 절반 (별지점 - 0.01) LOC
  - 후반전(T >= 분할수/2): 전부 (별지점 - 0.01) LOC
  - 소진(T >= 분할수 - 1): 매수 중단, 매도 주문만
  - 매도: 1/4 별지점 LOC, 3/4 평단 x (1 + 목표%) 지정가
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)

PHASE_LABELS = {
    "first": "첫 매수",
    "first_half": "전반전",
    "second_half": "후반전",
    "exhausted": "소진",
}

LEG_LABELS = {
    "first": "첫매수LOC",
    "avg": "평단LOC",
    "star": "별LOC",
    "quarter": "쿼터LOC",
    "target": "목표지정가",
}


def tick(price: Decimal) -> Decimal:
    """미국 주식 가격 단위: $1 이상 0.01, $1 미만 0.0001."""
    return Decimal("0.01") if price >= ONE else Decimal("0.0001")


def floor_price(price: Decimal) -> Decimal:
    return price.quantize(tick(price), rounding=ROUND_FLOOR)


def ceil_price(price: Decimal) -> Decimal:
    return price.quantize(tick(price), rounding=ROUND_CEILING)


def round_price(price: Decimal) -> Decimal:
    return price.quantize(tick(price), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Params:
    splits: int = 20
    target_pct: Decimal = Decimal(15)
    star_base_pct: Decimal | None = None
    star_slope: Decimal | None = None
    first_buy_premium_pct: Decimal = Decimal(12)
    extra_buy_levels: int = 0
    quarter_sell: bool = True

    @property
    def base(self) -> Decimal:
        return self.target_pct if self.star_base_pct is None else self.star_base_pct

    @property
    def slope(self) -> Decimal:
        if self.star_slope is not None:
            return self.star_slope
        return self.base * 2 / Decimal(self.splits)


@dataclass(frozen=True)
class PlannedOrder:
    leg: str
    side: str  # BUY | SELL
    tif: str  # CLS(LOC) | DAY(지정가)
    price: Decimal
    qty: int

    @property
    def amount(self) -> Decimal:
        return self.price * self.qty

    @property
    def label(self) -> str:
        if self.leg.startswith("extra"):
            return "추가LOC"
        return LEG_LABELS.get(self.leg, self.leg)


@dataclass
class Plan:
    phase: str
    qty: Decimal
    avg: Decimal
    last_price: Decimal
    unit: Decimal
    t: Decimal
    star_pct: Decimal | None
    star_price: Decimal | None
    orders: list[PlannedOrder] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def buys(self) -> list[PlannedOrder]:
        return [o for o in self.orders if o.side == "BUY"]

    @property
    def sells(self) -> list[PlannedOrder]:
        return [o for o in self.orders if o.side == "SELL"]


def t_value(qty: Decimal, avg: Decimal, unit: Decimal) -> Decimal:
    if unit <= 0:
        raise ValueError("1회 매수금은 0보다 커야 합니다")
    return (qty * avg / unit).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def star_pct(t: Decimal, params: Params) -> Decimal:
    return params.base - params.slope * t


def build_plan(
    *,
    qty: Decimal,
    avg: Decimal,
    last_price: Decimal,
    unit: Decimal,
    params: Params,
    sellable: Decimal | None = None,
    cash: Decimal | None = None,
    budget_left: Decimal | None = None,
) -> Plan:
    whole = int(qty)
    if whole < 1:
        return _first_buy_plan(qty, avg, last_price, unit, params, cash, budget_left)

    t = t_value(qty, avg, unit)
    sp = star_pct(t, params)
    star_price = round_price(avg * (ONE + sp / HUNDRED))
    buy_ceiling = star_price - tick(star_price)
    plan = Plan("", qty, avg, last_price, unit, t, sp, star_price)

    if t >= params.splits - 1:
        plan.phase = "exhausted"
        plan.notes.append(
            f"T가 {params.splits - 1} 이상이라 매수를 멈추고 매도 주문만 냅니다. "
            "리버스모드/손절 여부는 직접 판단하세요."
        )
    elif buy_ceiling <= 0:
        plan.phase = "second_half"
        plan.notes.append("별지점이 0 이하라 매수 주문을 내지 않습니다. 파라미터를 확인하세요.")
    elif t < Decimal(params.splits) / 2:
        plan.phase = "first_half"
        half = unit / 2
        # 평단 LOC가 쿼터매도 LOC와 같은 가격이 되지 않도록 별지점-0.01을 상한으로 둔다.
        avg_price = min(floor_price(avg), buy_ceiling)
        q_avg = int(half // avg_price)
        q_star = int(half // buy_ceiling)
        if q_avg + q_star == 0:
            q_star = int(unit // buy_ceiling)
            if q_star:
                plan.notes.append("매수금 절반으로는 1주도 못 사서 1회 매수금 전부를 별LOC로 냅니다.")
        if q_avg:
            plan.orders.append(PlannedOrder("avg", "BUY", "CLS", avg_price, q_avg))
        if q_star:
            plan.orders.append(PlannedOrder("star", "BUY", "CLS", buy_ceiling, q_star))
    else:
        plan.phase = "second_half"
        q = int(unit // buy_ceiling)
        if q:
            plan.orders.append(PlannedOrder("star", "BUY", "CLS", buy_ceiling, q))

    if plan.phase != "exhausted" and not plan.buys:
        plan.notes.append("1회 매수금이 주가보다 작아 매수 주문이 없습니다.")

    _cap_buys(plan, cash, budget_left)
    _add_extra_levels(plan, params, unit, cash)
    _add_sells(plan, params, whole, sellable, star_price, avg)
    return plan


def _first_buy_plan(qty, avg, last_price, unit, params, cash, budget_left) -> Plan:
    plan = Plan("first", qty, avg, last_price, unit, ZERO, None, None)
    if qty > 0:
        plan.notes.append(f"1주 미만 잔량({qty}주)은 무시하고 새 사이클로 시작합니다.")
    limit = round_price(last_price * (ONE + params.first_buy_premium_pct / HUNDRED))
    n = int(unit // last_price)
    if n < 1:
        plan.notes.append("1회 매수금이 현재가보다 작아 첫 매수를 할 수 없습니다.")
        return plan
    plan.orders.append(PlannedOrder("first", "BUY", "CLS", limit, n))
    # 첫 매수는 종가에 체결되므로 현금 한도는 현재가 기준으로 본다.
    _cap_buys(plan, cash, budget_left, price_of=lambda o: last_price)
    _add_extra_levels(plan, params, unit, cash)
    return plan


def _cap_buys(plan: Plan, cash, budget_left, price_of=lambda o: o.price) -> None:
    caps = []
    if cash is not None:
        caps.append((cash, "매수가능금액"))
    if budget_left is not None:
        caps.append((budget_left, "사이클 잔여예산"))
    if not caps:
        return
    cap, cap_label = min(caps, key=lambda c: c[0])
    orders = list(plan.orders)
    total = sum((price_of(o) * o.qty for o in orders if o.side == "BUY"), ZERO)
    if total <= cap:
        return
    while total > cap:
        idx = [i for i, o in enumerate(orders) if o.side == "BUY" and o.qty > 0]
        if not idx:
            break
        i = max(idx, key=lambda j: orders[j].price)
        total -= price_of(orders[i])
        orders[i] = replace(orders[i], qty=orders[i].qty - 1)
    plan.orders = [o for o in orders if o.qty > 0]
    plan.notes.append(f"{cap_label}(${cap:,.2f}) 한도에 맞춰 매수 수량을 줄였습니다.")


def _add_extra_levels(plan: Plan, params: Params, unit: Decimal, cash) -> None:
    """하락 추가매수: 종가가 더 떨어지면 1회 매수금으로 더 많은 주식을 사도록 1주씩 LOC."""
    if params.extra_buy_levels <= 0 or not plan.buys:
        return
    if cash is not None and cash < unit:
        return
    base = sum(o.qty for o in plan.buys)
    lowest = min(o.price for o in plan.buys)
    for k in range(1, params.extra_buy_levels + 1):
        price = floor_price(unit / (base + k))
        if ZERO < price < lowest:
            plan.orders.append(PlannedOrder(f"extra{k}", "BUY", "CLS", price, 1))
            lowest = price


def _add_sells(plan: Plan, params: Params, whole: int, sellable, star_price, avg) -> None:
    n = whole
    if sellable is not None and int(sellable) < whole:
        n = int(sellable)
        plan.notes.append(f"매도가능수량 {n}주 기준으로 매도합니다 (보유 {whole}주).")
    if n < 1:
        return
    quarter = n // 4 if params.quarter_sell else 0
    rest = n - quarter
    target_price = ceil_price(avg * (ONE + params.target_pct / HUNDRED))
    if quarter:
        plan.orders.append(PlannedOrder("quarter", "SELL", "CLS", star_price, quarter))
    if rest:
        plan.orders.append(PlannedOrder("target", "SELL", "DAY", target_price, rest))
