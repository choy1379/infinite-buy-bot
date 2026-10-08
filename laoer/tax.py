"""올해 해외주식 양도소득세 대략치.

토스 Open API 에는 양도세 조회가 없어서, 올해 1월부터의 종료 주문(체결)으로 직접 근사한다.
  원화 손익 = (매도가 × 매도일 환율) - (산 가격 × 산 날 환율)   (수수료·세금은 필요경비로 뺌)
  양도세   = (올해 합계 - 기본공제 250만원) × 22%   (지방소득세 포함)
취득가는 선입선출(먼저 산 것부터 판 것으로 봄)로 계산한다. 올해 이전에 산 물량을 판 건 평단을 몰라서 빼고, 몇 건인지 따로 알려준다.
신고용이 아니라 감을 잡는 용도다 (정확한 값은 증권사 앱의 양도세 조회로 확인).
  python -m laoer tax   -> 지금 다시 계산해서 보여줌
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .realized import KST, _kst_date, _normalize_fill

log = logging.getLogger(__name__)

DEDUCTION = Decimal(2_500_000)
RATE = Decimal("0.22")
WON = Decimal("1")
STEP_DAYS = 15  # 한 번에 조회하는 구간 (길게 받으면 서버가 앞부분만 준다)
MAX_AGE = timedelta(hours=6)


class TaxLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {"fx": {}, "result": None, "at": None}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def fx(self) -> dict:
        return self.data.setdefault("fx", {})

    def stale(self, now: datetime) -> bool:
        at = self.data.get("at")
        if not at or not self.data.get("result"):
            return True
        return now - datetime.fromisoformat(at) >= MAX_AGE


def year_orders(toss, now: datetime) -> list[dict]:
    """올해 1월 1일부터 지금까지의 종료 주문을 구간별로 모은다 (주문번호로 중복 제거)."""
    today = now.astimezone(KST).date()
    floor = date(today.year, 1, 1)
    seen: dict[str, dict] = {}
    end = today
    while end >= floor:
        begin = max(end - timedelta(days=STEP_DAYS - 1), floor)
        for o in toss.closed_orders(start=begin.isoformat(), end=end.isoformat()):
            seen[str(o.get("orderId"))] = o
        end = begin - timedelta(days=1)
    return list(seen.values())


def compute(orders: list[dict], fx_of) -> dict:
    """체결 주문 → 올해 원화 손익과 예상 양도세. fx_of(filledAt) 는 그 시각의 원/달러 환율."""
    fills = []
    for o in orders:
        for side in ("BUY", "SELL"):
            f = _normalize_fill(o, side)
            if f and f["currency"] != "KRW":
                f["side"] = side
                fills.append(f)
    fills.sort(key=lambda f: f["filledAt"] or "")
    lots: dict[str, deque] = {}  # 종목 -> 산 순서대로 [남은 수량, 주당 원화 취득가] (선입선출)
    by_symbol: dict[str, Decimal] = {}
    unknown: dict[str, int] = {}
    total = Decimal(0)
    for f in fills:
        fx = fx_of(f["filledAt"])
        q = lots.setdefault(f["symbol"], deque())
        if f["side"] == "BUY":
            q.append([f["qty"], (f["qty"] * f["price"] + f["commission"]) * fx / f["qty"]])
            continue
        if sum((lot[0] for lot in q), Decimal(0)) < f["qty"]:  # 올해 이전에 산 물량이 섞여 있어 취득가를 알 수 없다
            unknown[f["symbol"]] = unknown.get(f["symbol"], 0) + 1
            q.clear()
            continue
        cost, need = Decimal(0), f["qty"]
        while need > 0:
            lot = q[0]
            take = min(lot[0], need)
            cost += take * lot[1]
            lot[0] -= take
            need -= take
            if lot[0] == 0:
                q.popleft()
        gain = (f["qty"] * f["price"] - f["commission"] - f["tax"]) * fx - cost
        by_symbol[f["symbol"]] = by_symbol.get(f["symbol"], Decimal(0)) + gain
        total += gain
    taxable = max(Decimal(0), total - DEDUCTION)
    return {
        "year": datetime.now(KST).year,
        "gainKrw": str(total.quantize(WON, rounding=ROUND_HALF_UP)),
        "deductionKrw": str(DEDUCTION),
        "taxableKrw": str(taxable.quantize(WON, rounding=ROUND_HALF_UP)),
        "taxKrw": str((taxable * RATE).quantize(WON, rounding=ROUND_HALF_UP)),
        "bySymbol": {s: str(v.quantize(WON, rounding=ROUND_HALF_UP)) for s, v in sorted(by_symbol.items())},
        "unknown": unknown,
    }


def refresh(toss, tlog: TaxLog, now: datetime | None = None) -> dict:
    """주문을 다시 받아 계산하고 저장한다. 환율은 날짜별로 한 번만 조회해 파일에 남긴다."""
    now = now or datetime.now(KST)

    def fx_of(iso: str) -> Decimal:
        day = _kst_date(iso)
        if day not in tlog.fx:
            tlog.fx[day] = str(toss.exchange_rate(iso))
        return Decimal(tlog.fx[day])

    result = compute(year_orders(toss, now), fx_of)
    result["year"] = now.astimezone(KST).year
    tlog.data["result"] = result
    tlog.data["at"] = now.astimezone(KST).isoformat()
    tlog.save()
    return result


_lock = threading.Lock()


def sync_view(bot) -> dict | None:
    """대시보드 갱신 때 호출: 저장된 계산을 돌려주고, 오래됐으면 뒤에서 다시 계산한다 (환율 조회가 많아 대시보드를 붙잡지 않으려고)."""
    try:
        tlog = TaxLog(bot.cfg.run.state_dir / "tax.json")
    except Exception as e:
        log.warning("양도세 기록 파일을 못 읽었습니다: %s", e)
        return None
    now = datetime.now(KST)
    if tlog.stale(now) and _lock.acquire(blocking=False):

        def work():
            try:
                refresh(bot.toss, tlog, now)
            except Exception as e:
                log.warning("양도세 계산 실패: %s", e)
            finally:
                _lock.release()

        threading.Thread(target=work, daemon=True, name="tax-refresh").start()
    result = tlog.data.get("result")
    return {**result, "at": tlog.data.get("at")} if result else None
