"""매매 흐름: 미국 장 일정 확인 → 정규장 중 주문 → 마감 후 체결 확인 → 알림."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal

from .config import Config
from .notify import Message, Notifier
from .state import State
from .strategy import LEG_LABELS, PHASE_LABELS, Plan, build_plan, t_value
from .toss import OPEN_STATUSES, TossError

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9), "KST")
ZERO = Decimal(0)
CENT = Decimal("0.01")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _d(v) -> Decimal:
    return Decimal(str(v)) if v not in (None, "") else ZERO


def usd(v) -> str:
    v = _d(v)
    return f"-${-v:,.2f}" if v < 0 else f"${v:,.2f}"


def pct(fraction) -> str:
    return f"{_d(fraction) * 100:+.2f}%"


def qty_str(v) -> str:
    return format(_d(v).normalize(), "f")


def kst(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%m/%d %H:%M")


def leg_label(leg: str) -> str:
    return "추가LOC" if leg.startswith("extra") else LEG_LABELS.get(leg, leg)


def side_label(side: str) -> str:
    return "매수" if side == "BUY" else "매도"


@dataclass(frozen=True)
class Session:
    date: str  # 미국 현지 영업일 YYYY-MM-DD
    start: datetime
    end: datetime


def parse_sessions(calendar: dict) -> list[Session]:
    found: dict[str, Session] = {}
    for key in ("previousBusinessDay", "today", "nextBusinessDay"):
        day = calendar.get(key) or {}
        reg = day.get("regularMarket")
        if day.get("date") and reg:
            found[day["date"]] = Session(day["date"], _dt(reg["startTime"]), _dt(reg["endTime"]))
    return sorted(found.values(), key=lambda s: s.start)


class Bot:
    def __init__(self, cfg: Config, toss, notifier: Notifier, state: State, *, clock=None, sleep=time.sleep):
        self.cfg = cfg
        self.toss = toss
        self.notifier = notifier
        self.state = state
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self._cal: list[Session] = []
        self._cal_fetched: datetime | None = None
        self._error_sent: dict[str, datetime] = {}

    @property
    def symbol(self) -> str:
        return self.cfg.symbol

    # --------------------------------------------------------------- calendar
    def sessions(self, *, refresh: bool = False) -> list[Session]:
        now = self.clock()
        if refresh or self._cal_fetched is None or now - self._cal_fetched > timedelta(minutes=20):
            self._cal = parse_sessions(self.toss.market_calendar_us())
            self._cal_fetched = now
        return self._cal

    def next_session(self) -> Session | None:
        """아직 끝나지 않은 가장 가까운 정규장."""
        now = self.clock()
        for refresh in (False, True):
            for s in self.sessions(refresh=refresh):
                if s.end > now:
                    return s
        return None

    def order_time(self, s: Session) -> datetime:
        return s.start + timedelta(minutes=self.cfg.run.order_offset_minutes)

    def cutoff_time(self, s: Session) -> datetime:
        return s.end - timedelta(minutes=self.cfg.run.order_cutoff_minutes)

    def report_due(self, end: datetime) -> datetime:
        due = end + timedelta(minutes=self.cfg.run.report_offset_minutes)
        not_before = self.cfg.run.report_not_before_kst
        if not_before:
            gate = end.astimezone(KST).replace(hour=not_before[0], minute=not_before[1], second=0, microsecond=0)
            due = max(due, gate)
        return due

    # ------------------------------------------------------------------ cycle
    def _new_cycle(self, date: str, *, qty=ZERO, avg=ZERO, mutate: bool = True) -> dict:
        seed = self.cfg.capital_usd
        if self.cfg.compound and self.state.history:
            last = self.state.history[-1]
            seed = _d(last["seed"]) + _d(last["profit"])
            if seed <= 0:
                seed = self.cfg.capital_usd
        unit = (seed / self.cfg.params.splits).quantize(CENT, rounding=ROUND_FLOOR)
        cost = (qty * avg).quantize(CENT)
        cycle = {
            "id": self.state.next_cycle_id(),
            "started": date,
            "seed": str(seed),
            "unit": str(unit),
            "bought": str(cost),
            "sold": "0",
            "fees": "0",
            "adopted": bool(cost > 0),
            "trading_days": 0,
        }
        if mutate:
            self.state.cycle = cycle
        return cycle

    def _finish_cycle(self, cycle: dict, date: str, *, outside: bool = False) -> dict:
        profit = _d(cycle["sold"]) - _d(cycle["bought"]) - _d(cycle["fees"])
        done = {**cycle, "ended": date, "profit": str(profit), "closed_outside": outside}
        self.state.history.append(done)
        self.state.cycle = None
        return done

    def _cycle_for(self, qty: Decimal, avg: Decimal, date: str, *, mutate: bool) -> tuple[dict, list[str]]:
        notes: list[str] = []
        cycle = self.state.cycle
        if int(qty) < 1:
            if cycle and _d(cycle.get("bought")) > 0:
                # 체결 리포트 없이 전량 매도된 경우(봇 중단 등) 여기서 사이클을 마감한다.
                if mutate:
                    self.notifier.send(self._cycle_message(self._finish_cycle(cycle, date, outside=True)))
                cycle = None
            if cycle is None:
                cycle = self._new_cycle(date, mutate=mutate)
        elif cycle is None:
            cycle = self._new_cycle(date, qty=qty, avg=avg, mutate=mutate)
            notes.append(f"기존 보유 {qty_str(qty)}주(평단 {usd(avg)})를 이어받아 사이클을 시작합니다.")
        return cycle, notes

    # ------------------------------------------------------------------- plan
    def make_plan(self, date: str, *, mutate: bool) -> tuple[Plan, dict, Decimal]:
        price = self.toss.price(self.symbol)
        h = self.toss.holding(self.symbol)
        qty = h.qty if h else ZERO
        avg = h.avg if h else ZERO
        cycle, notes = self._cycle_for(qty, avg, date, mutate=mutate)
        unit, seed = _d(cycle["unit"]), _d(cycle["seed"])
        cash = self.toss.buying_power("USD")
        sellable = self.toss.sellable_quantity(self.symbol) if int(qty) >= 1 else None
        plan = build_plan(
            qty=qty,
            avg=avg,
            last_price=price,
            unit=unit,
            params=self.cfg.params,
            sellable=sellable,
            cash=cash,
            budget_left=max(ZERO, seed - qty * avg),
        )
        plan.notes[:0] = notes
        return plan, cycle, cash

    def preview(self) -> Message:
        s = self.next_session()
        date = s.date if s else self.clock().astimezone(KST).date().isoformat()
        plan, cycle, cash = self.make_plan(date, mutate=False)
        rec = {
            "status": "preview",
            "cash": str(cash),
            "orders": [{"leg": o.leg, "side": o.side, "price": str(o.price), "qty": o.qty} for o in plan.orders],
        }
        msg = self._placed_message(date, plan, rec, cycle)
        if s:
            msg.lines.append(f"주문 예정 {kst(self.order_time(s))} KST · 정규장 {kst(s.start)}~{kst(s.end)}")
        return msg

    # ------------------------------------------------------------------ place
    def _client_order_id(self, date: str, leg: str, attempt: int) -> str:
        cid = f"lab-{self.symbol}-{date.replace('-', '')}-{leg}"
        if attempt > 1:
            cid += f"-r{attempt}"
        return re.sub(r"[^A-Za-z0-9_-]", "_", cid)[:36]

    def _cancel_open(self, day: dict) -> None:
        for e in day.get("orders", []):
            if not e.get("order_id") or e.get("canceled"):
                continue
            try:
                if self.toss.get_order(e["order_id"]).get("status") in OPEN_STATUSES:
                    self.toss.cancel_order(e["order_id"])
                    e["canceled"] = True
            except TossError as err:
                log.warning("기존 주문 %s 취소 실패: %s", e["order_id"], err)

    def place(self, session: Session, *, force: bool = False) -> dict:
        date = session.date
        mmdd = date[5:].replace("-", "/")
        prev = self.state.days.get(date)
        if prev and not force:
            log.info("%s 은 이미 처리됨 (%s)", date, prev.get("status"))
            return prev
        attempt = prev.get("attempt", 1) + 1 if prev else 1
        kept: list[dict] = []
        if prev:
            self._cancel_open(prev)
            kept = [dict(e, replaced=True) for e in prev.get("orders", []) if e.get("order_id")]

        if self.cfg.run.skip_if_open_orders and not force:
            opens = self.toss.open_orders(self.symbol)
            if opens:
                rec = {"status": "blocked", "attempt": attempt, "session_end": session.end.isoformat(), "open_orders": len(opens)}
                self.state.days[date] = rec
                self.state.save()
                self.notifier.send(
                    Message(
                        f"⚠️ {self.symbol} 오늘 주문 건너뜀 · {mmdd}",
                        [
                            f"이미 미체결 {self.symbol} 주문이 {len(opens)}건 있어 봇 주문을 내지 않았습니다.",
                            "수동 주문을 정리한 뒤 `python -m laoer order --force` 로 낼 수 있습니다.",
                        ],
                        "warn",
                    )
                )
                return rec

        plan, cycle, cash = self.make_plan(date, mutate=True)
        dry = self.cfg.run.dry_run
        rec = {
            "status": "dry_run" if dry else "placing",
            "attempt": attempt,
            "session_start": session.start.isoformat(),
            "session_end": session.end.isoformat(),
            "placed_at": self.clock().isoformat(),
            "cycle_id": cycle["id"],
            "phase": plan.phase,
            "t": str(plan.t),
            "star_pct": None if plan.star_pct is None else str(plan.star_pct),
            "star_price": None if plan.star_price is None else str(plan.star_price),
            "unit": str(plan.unit),
            "qty_before": str(plan.qty),
            "avg_before": str(plan.avg),
            "price": str(plan.last_price),
            "cash": str(cash),
            "notes": plan.notes,
            "orders": kept,
            "reported": False,
            "report_attempts": 0,
        }
        if not prev:
            cycle["trading_days"] = cycle.get("trading_days", 0) + 1
        self.state.days[date] = rec
        self.state.save()  # 주문 도중 죽어도 흔적이 남도록 먼저 저장

        failures = 0
        for o in plan.orders:
            e = {"leg": o.leg, "side": o.side, "tif": o.tif, "price": str(o.price), "qty": o.qty}
            if not dry:
                e["client_order_id"] = self._client_order_id(date, o.leg, attempt)
                try:
                    e["order_id"] = self.toss.place_order(
                        symbol=self.symbol, side=o.side, tif=o.tif, qty=o.qty, price=o.price, client_order_id=e["client_order_id"]
                    )
                    log.info("주문 접수 %s %s %s주 @ %s → %s", o.side, o.leg, o.qty, o.price, e["order_id"])
                except TossError as err:
                    e["error"] = str(err)
                    failures += 1
                    log.error("주문 실패 %s %s: %s", o.side, o.leg, err)
            rec["orders"].append(e)
            self.state.save()

        if not dry:
            rec["status"] = "failed" if plan.orders and failures == len(plan.orders) else "placed"
        self.state.save()
        self.notifier.send(self._placed_message(date, plan, rec, cycle))
        return rec

    # ----------------------------------------------------------------- report
    def report(self, date: str, *, final: bool = False) -> str:
        day = self.state.days[date]
        pending = False
        for e in day.get("orders", []):
            if not e.get("order_id") or e.get("final"):
                continue
            try:
                od = self.toss.get_order(e["order_id"])
            except TossError as err:
                log.warning("주문 %s 조회 실패: %s", e["order_id"], err)
                pending = True
                continue
            ex = od.get("execution") or {}
            e["status"] = od.get("status")
            e["filled_qty"] = str(_d(ex.get("filledQuantity")))
            e["filled_avg"] = ex.get("averageFilledPrice")
            e["filled_amount"] = str(_d(ex.get("filledAmount")))
            e["fees"] = str(_d(ex.get("commission")) + _d(ex.get("tax")))
            if e["status"] in OPEN_STATUSES:
                pending = True
            else:
                e["final"] = True
        day["report_attempts"] = day.get("report_attempts", 0) + 1
        if pending and not final and day["report_attempts"] < 6:
            self.state.save()
            return "pending"

        cycle = self.state.cycle
        if cycle and cycle.get("id") == day.get("cycle_id") and not day.get("applied"):
            for e in day.get("orders", []):
                key = "bought" if e["side"] == "BUY" else "sold"
                cycle[key] = str(_d(cycle[key]) + _d(e.get("filled_amount")))
                cycle["fees"] = str(_d(cycle["fees"]) + _d(e.get("fees")))
            day["applied"] = True

        h = self.toss.holding(self.symbol)
        qty = h.qty if h else ZERO
        day.update(reported=True, qty_after=str(qty), avg_after=str(h.avg if h else ZERO))
        sold_qty = sum((_d(e.get("filled_qty")) for e in day.get("orders", []) if e["side"] == "SELL"), ZERO)
        completed = None
        if cycle and int(qty) < 1 and _d(cycle["bought"]) > 0 and sold_qty > 0:
            completed = self._finish_cycle(cycle, date)
        self.state.save()
        self.notifier.send(self._report_message(date, day, h, cycle))
        if completed:
            self.notifier.send(self._cycle_message(completed))
        return "done"

    # --------------------------------------------------------------- messages
    def _placed_message(self, date: str, plan: Plan, rec: dict, cycle: dict) -> Message:
        status = rec["status"]
        head = {"dry_run": "🧪 [모의] ", "preview": "🔎 [미리보기] "}.get(status, "📝 ")
        phase = PHASE_LABELS.get(plan.phase, plan.phase)
        title = f"{head}{self.symbol} 주문 · {date[5:].replace('-', '/')} · {phase}"
        if plan.phase != "first":
            title += f" T{plan.t}"
        lines = []
        if plan.phase == "first":
            lines.append(f"사이클 #{cycle['id']} 시작 · 원금 {usd(cycle['seed'])} · 1회 {usd(plan.unit)}")
            lines.append(f"현재가 {usd(plan.last_price)}")
        else:
            pl = plan.last_price / plan.avg - 1 if plan.avg else ZERO
            lines.append(f"보유 {qty_str(plan.qty)}주 · 평단 {usd(plan.avg)} · 현재가 {usd(plan.last_price)} ({pct(pl)})")
            lines.append(
                f"T {plan.t}/{self.cfg.params.splits} · 별% {plan.star_pct:+.2f}% · 별지점 {usd(plan.star_price)} · 1회 {usd(plan.unit)}"
            )
        new_orders = [e for e in rec["orders"] if not e.get("replaced")]
        live = status in ("placed", "failed", "placing")
        for e in new_orders:
            mark = (" ✅" if e.get("order_id") else f" ❌ {e.get('error', '')}") if live else ""
            lines.append(f"{side_label(e['side'])} {leg_label(e['leg'])} {e['qty']}주 @ {usd(e['price'])}{mark}")
        if not new_orders:
            lines.append("낼 주문이 없습니다.")
        lines += [f"※ {n}" for n in plan.notes]
        lines.append(f"매수가능 {usd(rec['cash'])}")
        if status == "dry_run":
            lines.append("모의 실행이라 실제 주문은 넣지 않았습니다. (run.dry_run = false 로 실거래)")
        errors = sum(1 for e in new_orders if e.get("error"))
        level = "error" if errors and errors == len(new_orders) else "warn" if errors or plan.notes else "info"
        return Message(title, lines, level)

    def _report_message(self, date: str, day: dict, h, cycle: dict | None) -> Message:
        dry = day.get("status") == "dry_run"
        title = f"📊 {self.symbol} 체결 결과 · {date[5:].replace('-', '/')}" + (" (모의)" if dry else "")
        lines = ["모의 실행이라 체결 내역은 없습니다."] if dry else []
        filled_any = False
        for e in day.get("orders", []):
            if not e.get("order_id"):
                continue
            fq = _d(e.get("filled_qty"))
            label = f"{side_label(e['side'])} {leg_label(e['leg'])}"
            if fq > 0:
                filled_any = True
                lines.append(f"✅ {label} {qty_str(fq)}/{e['qty']}주 @ {usd(e.get('filled_avg'))}")
            elif not e.get("replaced"):
                lines.append(f"▫️ {label} 미체결")
        if h and int(h.qty) >= 1:
            unit = _d(day.get("unit"))
            t_text = f" · T {t_value(h.qty, h.avg, unit)}/{self.cfg.params.splits}" if unit > 0 else ""
            lines.append(f"보유 {qty_str(h.qty)}주 · 평단 {usd(h.avg)} · 현재가 {usd(h.last_price)}")
            lines.append(f"평가손익 {pct(h.pl_rate)}{t_text}")
        else:
            lines.append("보유 0주")
        if cycle:
            lines.append(f"사이클 #{cycle['id']} · {cycle.get('trading_days', 0)}거래일째")
        return Message(title, lines, "success" if filled_any else "info")

    def _cycle_message(self, done: dict) -> Message:
        profit, seed = _d(done["profit"]), _d(done["seed"])
        lines = [
            f"매수 {usd(done['bought'])} · 매도 {usd(done['sold'])} · 비용 {usd(done['fees'])}",
            f"실현손익 {usd(profit)} (원금 대비 {pct(profit / seed if seed else 0)})",
            f"기간 {done['started']} ~ {done['ended']} · {done.get('trading_days', 0)}거래일",
            "다음 정규장부터 새 사이클을 시작합니다.",
        ]
        if done.get("adopted"):
            lines.insert(0, "※ 기존 보유분을 이어받은 사이클이라 이어받을 때의 평단 기준 손익입니다.")
        if done.get("closed_outside"):
            lines.insert(0, "※ 봇이 확인하지 못한 체결(수동 매도 등)로 끝나서 손익이 정확하지 않을 수 있습니다.")
        return Message(f"🎉 {self.symbol} 사이클 #{done['id']} 완료", lines, "success")

    def status(self) -> Message:
        h = self.toss.holding(self.symbol)
        price = self.toss.price(self.symbol)
        cash = self.toss.buying_power("USD")
        cycle = self.state.cycle
        lines = [f"계좌 {self.toss.account_seq} · {'모의(DRY-RUN)' if self.cfg.run.dry_run else '실거래(LIVE)'}"]
        unit = _d(cycle["unit"]) if cycle else (self.cfg.capital_usd / self.cfg.params.splits).quantize(CENT, ROUND_FLOOR)
        if h and int(h.qty) >= 1:
            lines.append(f"보유 {qty_str(h.qty)}주 · 평단 {usd(h.avg)} · 평가손익 {pct(h.pl_rate)}")
            lines.append(f"T {t_value(h.qty, h.avg, unit)}/{self.cfg.params.splits} · 1회 매수금 {usd(unit)}")
        else:
            lines.append("보유 0주 — 다음 정규장에 새 사이클 첫 매수")
        lines.append(f"현재가 {usd(price)} · 매수가능 {usd(cash)}")
        if cycle:
            lines.append(
                f"사이클 #{cycle['id']} · 시작 {cycle['started']} · 원금 {usd(cycle['seed'])} · {cycle.get('trading_days', 0)}거래일째"
            )
        s = self.next_session()
        if s:
            lines.append(f"다음 정규장 {s.date}: {kst(s.start)}~{kst(s.end)} KST · 주문 {kst(self.order_time(s))}")
            day = self.state.days.get(s.date)
            if day:
                lines.append(f"해당일 상태: {day.get('status')}")
        if self.state.history:
            total = sum((_d(c["profit"]) for c in self.state.history), ZERO)
            lines.append(f"완료 사이클 {len(self.state.history)}개 · 누적 실현손익 {usd(total)}")
        return Message(f"ℹ️ {self.symbol} 무매봇 상태", lines)

    # -------------------------------------------------------------- scheduler
    def tick(self) -> float:
        """할 일을 하고, 다음에 깨어날 때까지의 초를 돌려준다."""
        now = self.clock()
        run = self.cfg.run
        for date in sorted(self.state.days):
            day = self.state.days[date]
            if day.get("status") == "placing":
                day["status"] = "placed"
                self.state.save()
                self.notifier.send(
                    Message(
                        f"⚠️ {self.symbol} 주문 도중 중단된 기록 · {date[5:].replace('-', '/')}",
                        ["이전 실행이 주문을 내던 중 멈췄습니다.", "토스증권 앱에서 주문이 중복/누락되지 않았는지 확인하세요."],
                        "warn",
                    )
                )
            if day.get("status") in ("placed", "dry_run") and not day.get("reported") and day.get("session_end"):
                if now >= self.report_due(_dt(day["session_end"])):
                    if self.report(date) == "pending":
                        return 300

        s = self.next_session()
        if s is None:
            return 3600
        order_at, cutoff = self.order_time(s), self.cutoff_time(s)
        day = self.state.days.get(s.date)
        if day is None:
            if now < order_at:
                return (order_at - now).total_seconds()
            if now < cutoff:
                rec = self.place(s)
                if rec.get("status") == "failed":
                    return 600
                return max(60.0, (self.report_due(s.end) - now).total_seconds())
            self.state.days[s.date] = {"status": "missed", "session_end": s.end.isoformat()}
            self.state.save()
            self.notifier.send(
                Message(
                    f"⚠️ {self.symbol} 오늘 주문 시간을 놓쳤습니다 · {s.date[5:].replace('-', '/')}",
                    [f"정규장 마감 {run.order_cutoff_minutes}분 전 이후에 봇이 실행돼 주문을 내지 않았습니다."],
                    "warn",
                )
            )
            return max(60.0, (s.end - now).total_seconds() + 60)
        if day.get("status") == "failed" and day.get("attempt", 1) < 3 and now < cutoff:
            self.place(s, force=True)
            return 600
        if day.get("status") in ("placed", "dry_run") and not day.get("reported"):
            return max(30.0, (self.report_due(s.end) - now).total_seconds())
        return max(60.0, (s.end - now).total_seconds() + 60)

    def _notify_error(self, err: Exception) -> None:
        key = str(err)[:200]
        now = self.clock()
        last = self._error_sent.get(key)
        if last and now - last < timedelta(hours=1):
            return
        self._error_sent[key] = now
        self.notifier.send(Message(f"🚨 {self.symbol} 무매봇 오류", [key, "5분 뒤 다시 시도합니다."], "error"))

    def run_forever(self) -> None:
        p = self.cfg.params
        mode = "모의(DRY-RUN)" if self.cfg.run.dry_run else "실거래(LIVE)"
        self.notifier.send(
            Message(
                f"🤖 {self.symbol} 무매봇 시작 · {mode}",
                [f"원금 {usd(self.cfg.capital_usd)} · {p.splits}분할 · 목표 {p.target_pct}% · 별% {p.base}-{p.slope:.3g}T"],
            )
        )
        try:
            while True:
                try:
                    wait = self.tick()
                except Exception as e:  # 네트워크/API 오류로 봇이 죽지 않게
                    log.exception("tick 실패")
                    self._notify_error(e)
                    wait = 300
                wait = min(max(wait, 30.0), 1800.0)
                log.info("다음 확인까지 %.0f초 대기", wait)
                self.sleep(wait)
        except KeyboardInterrupt:
            log.info("사용자 중지")
            self.notifier.send(Message(f"🛑 {self.symbol} 무매봇 종료", ["사용자가 봇을 멈췄습니다."], "warn"))
