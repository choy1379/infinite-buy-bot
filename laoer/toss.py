"""토스증권 Open API 클라이언트 (표준 라이브러리만 사용).

스펙: https://openapi.tossinvest.com (토스증권 Open API 1.1.x)
  - 인증: POST /oauth2/token (client_credentials) -> Authorization: Bearer
  - 계좌 컨텍스트 API는 X-Tossinvest-Account: {accountSeq} 헤더 필요
  - LOC 주문 = orderType LIMIT + timeInForce CLS (미국 주식만)
  - client 당 유효 토큰은 1개라서 재발급하면 이전 토큰이 즉시 무효 -> 파일에 캐시해 공유
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import threading
import time
import zlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openapi.tossinvest.com"
USER_AGENT = f"tecl-infinite-buy/{__version__}"
OPEN_STATUSES = {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"}


class TossError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str | None = None, data=None):
        text = f"[{status} {code}] {message}".rstrip()
        if request_id:
            text += f" (requestId={request_id})"
        super().__init__(text)
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.data = data


@dataclass(frozen=True)
class Holding:
    symbol: str
    qty: Decimal
    avg: Decimal
    last_price: Decimal
    purchase_amount: Decimal
    pl_rate: Decimal


def _decompress(raw: bytes, encoding: str | None) -> bytes:
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc or raw[:2] == b"\x1f\x8b":
            return gzip.decompress(raw)
        if "deflate" in enc:
            return zlib.decompress(raw)
    except (OSError, zlib.error, EOFError):
        pass
    return raw


def _parse(raw: bytes, encoding: str | None = None):
    if not raw:
        return {}
    raw = _decompress(raw, encoding)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = raw.decode("utf-8", "replace")
        if "<" in text and ">" in text:  # HTML 오류 페이지(방화벽 등)는 본문 글자만
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
            text = re.sub(r"<[^>]+>", " ", text)
        return {"raw": re.sub(r"\s+", " ", text).strip()[:300]}


def _error_from(status: int, payload) -> TossError:
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return TossError(status, err.get("code", "unknown"), err.get("message", ""), err.get("requestId"), err.get("data"))
    if isinstance(err, str):  # /oauth2/token 은 OAuth2 표준 포맷
        return TossError(status, err, payload.get("error_description", ""))
    return TossError(status, "http-error", str(payload)[:300])


def _retry_after(headers) -> float:
    try:
        return max(0.5, float(headers.get("Retry-After") or 1))
    except (TypeError, ValueError):
        return 1.0


class TossClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token_path: Path | None = None,
        account_seq: int | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        timeout: float = 15.0,
        max_attempts: int = 5,
        sleep=time.sleep,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.token_path = Path(token_path) if token_path else None
        self._account_seq = account_seq
        self._opener = opener or urllib.request.build_opener()
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._access_token: str | None = None
        self._token_lock = threading.Lock()  # 실시간 호가 서버 스레드와 봇이 토큰을 같이 씀

    # ------------------------------------------------------------------ auth
    def _load_cached_token(self) -> str | None:
        if not self.token_path or not self.token_path.exists():
            return None
        try:
            d = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if d.get("client_id") != self.client_id or d.get("expires_at", 0) - 300 < time.time():
            return None
        return d.get("access_token")

    def _save_token(self, token: str, expires_in: int) -> None:
        if not self.token_path:
            return
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"client_id": self.client_id, "access_token": token, "expires_at": time.time() + expires_in}),
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.token_path)

    def _issue_token(self) -> str:
        log.info("토스증권 access token 발급")
        d = self._call(
            "POST",
            "/oauth2/token",
            form={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret},
            auth=False,
        )
        token = d["access_token"]
        self._save_token(token, int(d.get("expires_in", 86400)))
        return token

    def _token(self, *, force: bool = False, stale: str | None = None) -> str:
        with self._token_lock:
            return self._token_locked(force=force, stale=stale)

    def _token_locked(self, *, force: bool, stale: str | None) -> str:
        # force 여도 다른 스레드가 이미 새 토큰으로 바꿨으면 그걸 씀 (한 번만 재발급)
        if self._access_token and (not force or (stale and self._access_token != stale)):
            return self._access_token
        cached = self._load_cached_token()
        if cached and cached != stale:
            self._access_token = cached
        else:
            self._access_token = self._issue_token()
        return self._access_token

    # ------------------------------------------------------------------ http
    def _http(self, method: str, url: str, headers: dict, data: bytes | None):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.headers, _parse(resp.read(), resp.headers.get("Content-Encoding"))
        except urllib.error.HTTPError as e:
            headers = e.headers or {}
            return e.code, e.headers, _parse(e.read(), headers.get("Content-Encoding"))

    def _call(self, method, path, *, params=None, json_body=None, form=None, auth=True, account=False, retry=True):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        reauthed = False
        attempt = 0
        while True:
            attempt += 1
            headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
            data = None
            if json_body is not None:
                data = json.dumps(json_body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            elif form is not None:
                data = urllib.parse.urlencode(form).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            token = None
            if auth:
                token = self._token()
                headers["Authorization"] = f"Bearer {token}"
            if account:
                headers["X-Tossinvest-Account"] = str(self.account_seq)
            try:
                status, resp_headers, payload = self._http(method, url, headers, data)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if retry and attempt < self.max_attempts:
                    log.warning("%s %s 네트워크 오류, 재시도 %d: %s", method, path, attempt, e)
                    self._sleep(min(2**attempt, 30))
                    continue
                raise TossError(0, "network-error", str(e)) from e

            if 200 <= status < 300:
                return payload
            if status == 401 and auth and not reauthed:
                reauthed = True
                self._token(force=True, stale=token)
                continue
            if status == 429 and attempt < self.max_attempts:
                wait = _retry_after(resp_headers)
                log.warning("%s %s 요청 한도 초과, %.1f초 후 재시도", method, path, wait)
                self._sleep(wait)
                continue
            if status >= 500 and retry and attempt < self.max_attempts:
                log.warning("%s %s 서버 오류 %d, 재시도 %d", method, path, status, attempt)
                self._sleep(min(2**attempt, 30))
                continue
            err = _error_from(status, payload)
            if err.code in ("http-error", "unknown"):
                err.args = (f"{method} {path} → {err}",)
            raise err

    # ------------------------------------------------------------- endpoints
    def accounts(self) -> list[dict]:
        return self._call("GET", "/api/v1/accounts")["result"]

    @property
    def account_seq(self) -> int:
        if self._account_seq is None:
            accts = self.accounts()
            brokerage = [a for a in accts if a.get("accountType", "BROKERAGE") == "BROKERAGE"] or accts
            if not brokerage:
                raise TossError(0, "no-account", "종합매매 계좌가 없습니다")
            self._account_seq = int(brokerage[0]["accountSeq"])
        return self._account_seq

    def quote(self, symbol: str) -> dict:
        """현재가 원본: {symbol, timestamp, lastPrice, currency}"""
        result = self._call("GET", "/api/v1/prices", params={"symbols": symbol})["result"]
        for r in result:
            if r.get("symbol", "").upper() == symbol.upper():
                return r
        raise TossError(404, "symbol-not-found", f"{symbol} 현재가가 없습니다")

    def price(self, symbol: str) -> Decimal:
        return Decimal(self.quote(symbol)["lastPrice"])

    def orderbook(self, symbol: str) -> dict:
        """호가: {timestamp, currency, asks: [{price, volume}] (낮은 가격순), bids (높은 가격순)}"""
        return self._call("GET", "/api/v1/orderbook", params={"symbol": symbol}, retry=False)["result"]

    def holding(self, symbol: str) -> Holding | None:
        try:
            result = self._call("GET", "/api/v1/holdings", params={"symbol": symbol}, account=True)["result"]
        except TossError as e:
            if e.status == 404:
                return None
            raise
        for it in result.get("items", []):
            if it.get("symbol", "").upper() == symbol.upper():
                return Holding(
                    symbol=it["symbol"],
                    qty=Decimal(it["quantity"]),
                    avg=Decimal(it["averagePurchasePrice"]),
                    last_price=Decimal(it["lastPrice"]),
                    purchase_amount=Decimal(it.get("marketValue", {}).get("purchaseAmount") or 0),
                    pl_rate=Decimal(it.get("profitLoss", {}).get("rate") or 0),
                )
        return None

    def holdings_all(self) -> dict:
        """계좌 전체 보유 종목 원본 {items: [...], 요약...}. 대시보드 '계좌 전체' 칸용"""
        try:
            return self._call("GET", "/api/v1/holdings", account=True)["result"]
        except TossError as e:
            if e.status == 404:
                return {"items": []}
            raise

    def buying_power(self, currency: str = "USD") -> Decimal:
        r = self._call("GET", "/api/v1/buying-power", params={"currency": currency}, account=True)["result"]
        return Decimal(r["cashBuyingPower"])

    def sellable_quantity(self, symbol: str) -> Decimal:
        try:
            r = self._call("GET", "/api/v1/sellable-quantity", params={"symbol": symbol}, account=True)["result"]
        except TossError as e:
            if e.status == 404:
                return Decimal(0)
            raise
        return Decimal(r["sellableQuantity"])

    def market_calendar_us(self) -> dict:
        return self._call("GET", "/api/v1/market-calendar/US")["result"]

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        r = self._call("GET", "/api/v1/orders", params={"status": "OPEN", "symbol": symbol}, account=True)["result"]
        return r.get("orders", [])

    def closed_orders(self, *, symbol: str | None = None, start: str | None = None, end: str | None = None, max_pages: int = 20) -> list[dict]:
        """종료된 주문(체결·취소 등) 목록. start/end 는 YYYY-MM-DD(KST, 주문시각 기준). 페이지를 끝까지 따라간다."""
        out: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            params = {"status": "CLOSED", "symbol": symbol, "from": start, "to": end, "cursor": cursor, "limit": 100}
            r = self._call("GET", "/api/v1/orders", params=params, account=True)["result"]
            out.extend(r.get("orders") or [])
            cursor = r.get("nextCursor")
            if not r.get("hasNext") or not cursor:
                break
        return out

    def exchange_rate(self, at: str | None = None, base: str = "USD", quote: str = "KRW") -> Decimal:
        """원/달러 환율. at(ISO 시각)을 주면 그 시점 환율."""
        r = self._call("GET", "/api/v1/exchange-rate", params={"baseCurrency": base, "quoteCurrency": quote, "dateTime": at})["result"]
        return Decimal(str(r["rate"]))

    def place_order(
        self, *, symbol: str, side: str, tif: str, qty: int, price: Decimal, client_order_id: str, order_type: str = "LIMIT"
    ) -> str:
        body = {
            "clientOrderId": client_order_id,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "timeInForce": tif,
            "quantity": str(int(qty)),
            "price": format(price, "f"),
        }
        # clientOrderId 가 10분간 멱등 키라서 재시도해도 중복 주문이 생기지 않는다.
        return self._call("POST", "/api/v1/orders", json_body=body, account=True)["result"]["orderId"]

    def get_order(self, order_id: str) -> dict:
        path = "/api/v1/orders/" + urllib.parse.quote(order_id, safe="")
        return self._call("GET", path, account=True)["result"]

    def cancel_order(self, order_id: str) -> str:
        path = "/api/v1/orders/" + urllib.parse.quote(order_id, safe="") + "/cancel"
        return self._call("POST", path, json_body={}, account=True)["result"]["orderId"]
