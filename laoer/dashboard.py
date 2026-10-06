"""모니터링 페이지용 dashboard.json 생성 + GitHub 업로드.

공개 저장소에 올라가므로 두 부분으로 나눈다.
  - public : T값, 수익률(%), 체결 여부, 봇 상태 — 누구나 볼 수 있음
  - secret : 금액·수량·평단·잔고·주문가 — 비밀번호로 암호화 (페이지에서 비밀번호를 넣으면 풀림)

암호화 (표준 라이브러리만, 브라우저 WebCrypto 로 풀 수 있게):
  key = PBKDF2-HMAC-SHA256(password, salt, iter, 64바이트) → enc_key(32) + mac_key(32)
  keystream 블록 i = HMAC-SHA256(enc_key, nonce || i(4바이트 big-endian))   (HMAC 카운터 모드)
  ct = plaintext XOR keystream,  mac = HMAC-SHA256(mac_key, nonce || ct)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

from . import __version__

log = logging.getLogger(__name__)

PBKDF2_ITER = 200_000
USER_AGENT = f"tecl-infinite-buy/{__version__}"


# ------------------------------------------------------------------ crypto
def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(enc_key, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def _derive(password: str, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
    k = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=64)
    return k[:32], k[32:]


def encrypt(password: str, plaintext: bytes, *, iterations: int = PBKDF2_ITER, salt: bytes | None = None) -> dict:
    salt = salt or os.urandom(16)
    nonce = os.urandom(16)
    enc_key, mac_key = _derive(password, salt, iterations)
    ct = bytes(a ^ b for a, b in zip(plaintext, _keystream(enc_key, nonce, len(plaintext))))
    mac = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return {"alg": "pbkdf2-sha256/hmac-ctr", "iter": iterations, "salt": b64(salt), "nonce": b64(nonce), "ct": b64(ct), "mac": b64(mac)}


def decrypt(password: str, box: dict) -> bytes:
    d = lambda k: base64.b64decode(box[k])
    salt, nonce, ct, mac = d("salt"), d("nonce"), d("ct"), d("mac")
    enc_key, mac_key = _derive(password, salt, int(box["iter"]))
    if not hmac.compare_digest(hmac.new(mac_key, nonce + ct, hashlib.sha256).digest(), mac):
        raise ValueError("비밀번호가 틀렸습니다")
    return bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct))))


# ----------------------------------------------------------------- payload
def _s(v) -> str | None:
    return None if v is None else str(v)


def _order_status(e: dict) -> str:
    if e.get("error"):
        return "error"
    if not e.get("order_id"):
        return "planned"
    fq = Decimal(str(e.get("filled_qty") or 0))
    if fq <= 0:
        return "pending" if not e.get("final") else "unfilled"
    return "filled" if fq >= Decimal(str(e.get("qty") or 0)) else "partial"


def build_payload(bot, *, holding=None, price=None, cash=None, session=None, password: str = "", days: int = 30) -> dict:
    """bot.state + 실시간 조회값으로 공개/비공개 데이터를 만든다."""
    from .bot import _d, kst  # 순환 import 방지
    from .strategy import t_value

    cfg = bot.cfg
    state = bot.state
    cycle = state.cycle
    unit = _d(cycle["unit"]) if cycle else Decimal(0)
    now = datetime.now(timezone.utc)

    t = None
    if holding and int(holding.qty) >= 1 and unit > 0:
        t = str(t_value(holding.qty, holding.avg, unit))

    pub_days, sec_days = [], []
    for date in sorted(state.days)[-days:][::-1]:
        day = state.days[date]
        orders = [e for e in day.get("orders", []) if not e.get("replaced")]
        pub_days.append(
            {
                "date": date,
                "status": day.get("status"),
                "phase": day.get("phase"),
                "t": day.get("t"),
                "starPct": day.get("star_pct"),
                "reported": bool(day.get("reported")),
                "orders": [
                    {"leg": e["leg"], "side": e["side"], "tif": e["tif"], "status": _order_status(e)} for e in orders
                ],
            }
        )
        sec_days.append(
            {
                "date": date,
                "price": day.get("price"),
                "qtyBefore": day.get("qty_before"),
                "avgBefore": day.get("avg_before"),
                "qtyAfter": day.get("qty_after"),
                "avgAfter": day.get("avg_after"),
                "orders": [
                    {
                        "leg": e["leg"],
                        "price": e.get("price"),
                        "qty": e.get("qty"),
                        "filledQty": e.get("filled_qty"),
                        "filledAvg": e.get("filled_avg"),
                        "error": e.get("error"),
                    }
                    for e in orders
                ],
            }
        )

    history_pub, history_sec = [], []
    for c in state.history[::-1][:50]:
        seed = _d(c.get("seed"))
        profit = _d(c.get("profit"))
        history_pub.append(
            {
                "id": c["id"],
                "started": c.get("started"),
                "ended": c.get("ended"),
                "tradingDays": c.get("trading_days", 0),
                "returnPct": None if c.get("alert") or not seed else str((profit / seed * 100).quantize(Decimal("0.01"))),
            }
        )
        history_sec.append({"id": c["id"], "seed": c.get("seed"), "bought": c.get("bought"), "sold": c.get("sold"), "fees": c.get("fees"), "profit": c.get("profit")})

    public = {
        "version": 1,
        "symbol": cfg.symbol,
        "updatedAt": now.isoformat(),
        "mode": bot.mode_label(),
        "splits": cfg.params.splits,
        "targetPct": str(cfg.params.target_pct),
        "bot": {
            "lastError": bot.last_error,
            "lastErrorAt": bot.last_error_at,
        },
        "position": {
            "hasPosition": bool(holding and int(holding.qty) >= 1),
            "t": t,
            "plRate": _s(holding.pl_rate) if holding else None,
        },
        "cycle": None
        if not cycle
        else {"id": cycle["id"], "started": cycle.get("started"), "tradingDays": cycle.get("trading_days", 0)},
        "nextSession": None
        if not session
        else {
            "date": session.date,
            "start": session.start.isoformat(),
            "end": session.end.isoformat(),
            "orderAt": bot.order_time(session).isoformat(),
        },
        "days": pub_days,
        "history": history_pub,
        "secret": None,
    }
    if password:
        secret = {
            "holding": None
            if not holding
            else {
                "qty": _s(holding.qty),
                "avg": _s(holding.avg),
                "lastPrice": _s(holding.last_price),
                "purchaseAmount": _s(holding.purchase_amount),
            },
            "price": _s(price),
            "cash": _s(cash),
            "cycle": None if not cycle else {k: cycle.get(k) for k in ("seed", "unit", "bought", "sold", "fees", "adopted")},
            "days": sec_days,
            "history": history_sec,
        }
        public["secret"] = encrypt(password, json.dumps(secret, ensure_ascii=False).encode("utf-8"))
    return public


# --------------------------------------------------------------- publisher
class GitHubPublisher:
    """GitHub Contents API 로 한 파일을 브랜치에 덮어쓴다 (git 설치 불필요)."""

    def __init__(self, token: str, repo: str, branch: str, path: str = "dashboard.json", *, opener=None, timeout: float = 20):
        self.token = token
        self.repo = repo
        self.branch = branch
        self.path = path
        self._opener = opener or urllib.request.build_opener()
        self.timeout = timeout
        self._branch_ok = False

    def _api(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}{path}",
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"message": raw[:200].decode("utf-8", "replace")}

    def _ensure_branch(self) -> None:
        if self._branch_ok:
            return
        status, _ = self._api("GET", f"/branches/{urllib.parse.quote(self.branch, safe='')}")
        if status == 404:
            status, repo = self._api("GET", "")
            if status != 200:
                raise RuntimeError(f"저장소 조회 실패 {status}: {repo.get('message')}")
            status, ref = self._api("GET", f"/git/ref/heads/{repo['default_branch']}")
            if status != 200:
                raise RuntimeError(f"기본 브랜치 조회 실패 {status}: {ref.get('message')}")
            status, res = self._api("POST", "/git/refs", {"ref": f"refs/heads/{self.branch}", "sha": ref["object"]["sha"]})
            if status not in (200, 201):
                raise RuntimeError(f"브랜치 생성 실패 {status}: {res.get('message')}")
            log.info("대시보드 브랜치 %s 생성", self.branch)
        elif status != 200:
            raise RuntimeError(f"브랜치 조회 실패 {status} — 토큰 권한(Contents: Read and write)을 확인하세요")
        self._branch_ok = True

    def put(self, content: bytes, message: str) -> None:
        self._ensure_branch()
        path = urllib.parse.quote(self.path)
        for _ in range(2):
            status, cur = self._api("GET", f"/contents/{path}?ref={urllib.parse.quote(self.branch, safe='')}")
            body = {"message": message, "content": base64.b64encode(content).decode("ascii"), "branch": self.branch}
            if status == 200:
                body["sha"] = cur["sha"]
            status, res = self._api("PUT", f"/contents/{path}", body)
            if status in (200, 201):
                return
            if status not in (409, 422):  # sha 경합이면 한 번 더
                break
        raise RuntimeError(f"대시보드 업로드 실패 {status}: {res.get('message')}")


class Dashboard:
    def __init__(self, publisher: GitHubPublisher, password: str, heartbeat_minutes: int = 180):
        self.publisher = publisher
        self.password = password
        self.heartbeat_minutes = heartbeat_minutes
        self.last_published: datetime | None = None

    def due(self, now: datetime) -> bool:
        return self.last_published is None or (now - self.last_published).total_seconds() >= self.heartbeat_minutes * 60

    def publish(self, bot, reason: str) -> None:
        h = price = cash = session = None
        try:
            h = bot.toss.holding(bot.symbol)
            price = bot.toss.price(bot.symbol)
            cash = bot.toss.buying_power("USD")
            session = bot.next_session()
        except Exception as e:  # 조회가 실패해도 상태만이라도 올린다
            log.warning("대시보드용 조회 실패: %s", e)
        payload = build_payload(bot, holding=h, price=price, cash=cash, session=session, password=self.password)
        self.publisher.put(json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"), f"dashboard: {reason}")
        self.last_published = bot.clock()
        log.info("대시보드 갱신 (%s)", reason)
