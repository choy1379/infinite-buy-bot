"""봇 밖에서(토스 앱 등) 판 종목의 실현 손익 기록.

토스 Open API 에는 '실현 손익' 조회가 없어서, 종료된 주문 목록(GET /orders?status=CLOSED)의 체결 정보와
보유 종목의 평균 매수가로 직접 계산한다.
  손익($) = (체결 평균가 - 평균 매수가) × 체결 수량 - 수수료 - 세금
  원화    = 손익($) × 체결 시각의 토스 환율   (토스 앱이 보여주는 원화 손익은 매수 때 환율까지 반영해서 조금 다를 수 있다)
한 번 기록하면 state/realized.json 에 남겨서, 나중에 평단이 바뀌거나 주문 조회 기간이 지나도 그대로 둔다.
  python -m laoer realized         -> 새로 판 내역을 기록하고 보여줌
  python -m laoer realized --raw   -> 최근 종료된 주문을 그대로 나열 (토스 앱 주문이 목록에 나오는지 확인용, 기록은 안 함)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
CENT = Decimal("0.01")
WON = Decimal("1")


def _d(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _s(v: Decimal | None) -> str | None:
    return None if v is None else format(v, "f")


class RealizedLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {"costs": {}, "sells": {}}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    @property
    def costs(self) -> dict:
        return self.data.setdefault("costs", {})

    @property
    def sells(self) -> dict:
        return self.data.setdefault("sells", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def view(self) -> dict:
        """대시보드(잠금 칸)용: 최근순 목록 + 통화별 합계."""
        items = sorted(self.sells.values(), key=lambda r: r.get("filledAt") or "", reverse=True)
        usd = [d for d in (_d(r.get("plUsd")) for r in items) if d is not None]
        krw = [d for d in (_d(r.get("plKrw")) for r in items) if d is not None]
        return {"items": items, "totalUsd": _s(sum(usd, Decimal(0))) if usd else None, "totalKrw": _s(sum(krw, Decimal(0))) if krw else None}


def normalize_sell(order: dict) -> dict | None:
    """종료된 주문 하나 → 체결된 매도면 정리한 dict, 아니면 None."""
    if str(order.get("side") or "").upper() != "SELL":
        return None
    ex = order.get("execution") or {}
    qty, price = _d(ex.get("filledQuantity")), _d(ex.get("averageFilledPrice"))
    if not qty or qty <= 0 or price is None:
        return None
    filled_at = ex.get("filledAt") or order.get("orderedAt")
    return {
        "orderId": str(order.get("orderId")),
        "symbol": order.get("symbol"),
        "currency": order.get("currency") or "USD",
        "qty": qty,
        "price": price,
        "amount": _d(ex.get("filledAmount")) or (qty * price),
        "commission": _d(ex.get("commission")) or Decimal(0),
        "tax": _d(ex.get("tax")) or Decimal(0),
        "filledAt": filled_at,
    }


def _kst_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:10]
    return (dt if dt.tzinfo else dt.replace(tzinfo=KST)).astimezone(KST).date().isoformat()


def build_record(sell: dict, cost: Decimal | None, fx: Decimal | None, name: str | None = None) -> dict:
    """체결 1건 → 기록. 평균 매수가나 환율을 모르면 해당 손익은 비워 둔다(화면에 '–')."""
    qty, price, fee, tax = sell["qty"], sell["price"], sell["commission"], sell["tax"]
    pl = None if cost is None else ((price - cost) * qty - fee - tax)
    krw_stock = sell["currency"] == "KRW"
    pl_usd = None if krw_stock or pl is None else pl.quantize(CENT, rounding=ROUND_HALF_UP)
    if krw_stock:
        pl_krw = None if pl is None else pl.quantize(WON, rounding=ROUND_HALF_UP)
    else:
        pl_krw = None if pl is None or fx is None else (pl * fx).quantize(WON, rounding=ROUND_HALF_UP)
    return {
        "orderId": sell["orderId"],
        "symbol": sell["symbol"],
        "name": name,
        "currency": sell["currency"],
        "date": _kst_date(sell["filledAt"]),
        "filledAt": sell["filledAt"],
        "qty": _s(qty),
        "price": _s(price),
        "cost": _s(cost),
        "commission": _s(fee),
        "tax": _s(tax),
        "fx": None if krw_stock else _s(fx),
        "plUsd": _s(pl_usd),
        "plKrw": _s(pl_krw),
    }


def sync(toss, rlog: RealizedLog, *, skip_symbol: str | None = None, days: int = 14, now: datetime | None = None) -> list[dict]:
    """최근 days일의 종료 주문에서 아직 기록 안 한 체결 매도를 찾아 기록한다. 봇 종목(skip_symbol)은 봇 쪽 기록이 따로 있어 뺀다."""
    now = now or datetime.now(KST)
    start = (now.astimezone(KST) - timedelta(days=days)).date().isoformat()
    try:
        held = (toss.holdings_all() or {}).get("items") or []
    except Exception as e:  # 평균 매수가를 못 읽어도 저장된 값으로 계속
        log.warning("실현손익: 보유 조회 실패: %s", e)
        held = []
    names = {}
    for it in held:
        sym, avg, qty = it.get("symbol"), _d(it.get("averagePurchasePrice")), _d(it.get("quantity"))
        if sym:
            names[sym] = it.get("name")
            if avg is not None and qty and qty > 0:
                rlog.costs[sym] = _s(avg)  # 팔아도 평균 매수가는 안 변하니, 다 팔기 전 마지막 값을 남겨 둔다
    new = []
    for order in toss.closed_orders(start=start):
        sell = normalize_sell(order)
        if not sell or sell["symbol"] == skip_symbol or sell["orderId"] in rlog.sells:
            continue
        fx = None
        if sell["currency"] != "KRW":
            try:
                fx = toss.exchange_rate(sell["filledAt"])
            except Exception as e:
                log.warning("실현손익: 환율 조회 실패(%s): %s", sell["symbol"], e)
        rec = build_record(sell, _d(rlog.costs.get(sell["symbol"])), fx, names.get(sell["symbol"]))
        rlog.sells[sell["orderId"]] = rec
        new.append(rec)
    if new or held:
        rlog.save()
    return new


def sync_view(bot) -> dict | None:
    """대시보드 갱신 때 호출: 새 매도를 기록하고 화면용 요약을 돌려준다. 실패해도 대시보드는 계속 올라가야 한다."""
    try:
        rlog = RealizedLog(bot.cfg.run.state_dir / "realized.json")
    except Exception as e:
        log.warning("실현손익 기록 파일을 못 읽었습니다: %s", e)
        return None
    try:
        sync(bot.toss, rlog, skip_symbol=bot.symbol)
    except Exception as e:  # 조회가 실패해도 이미 기록한 내역은 계속 보여 준다
        log.warning("실현손익 조회 실패: %s", e)
    view = rlog.view()
    return view if view["items"] else None
