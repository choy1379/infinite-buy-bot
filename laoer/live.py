"""집 PC(봇 PC)에서 띄우는 실시간 호가 페이지.

GitHub Pages 페이지는 봇이 몇 시간마다 올리는 파일만 볼 수 있어서 실시간 호가를 못 보여준다.
봇 PC는 토스 API를 부를 수 있으니, 같은 index.html 을 여기서 직접 띄우고
  /                -> index.html (GitHub Pages 와 같은 페이지)
  /dashboard.json  -> GitHub 의 dashboard.json 을 대신 받아 전달 (30초 캐시)
  /api/orderbook   -> 토스 GET /api/v1/orderbook (1초 캐시: 여러 기기가 봐도 토스 호출은 초당 1회)
를 제공한다. 페이지는 /api/orderbook 이 응답하면 '실시간 호가' 칸을 보여준다.
공개 시세와 이미 공개된 dashboard.json 만 내보내고, 계좌 정보는 다루지 않는다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

PAGE = Path(__file__).resolve().parents[1] / "index.html"


class _Cache:
    """ttl 동안 마지막 결과(또는 오류)를 재사용. 동시에 들어온 요청은 한 번만 가져온다."""

    def __init__(self, fetch, ttl: float, clock=time.monotonic):
        self.fetch, self.ttl, self.clock = fetch, ttl, clock
        self.lock = threading.Lock()
        self.at: float | None = None
        self.value = None
        self.error: Exception | None = None

    def get(self):
        with self.lock:
            if self.at is None or self.clock() - self.at >= self.ttl:
                try:
                    self.value, self.error = self.fetch(), None
                except Exception as e:  # 오류도 ttl 동안 캐시해 토스 호출이 몰리지 않게
                    self.value, self.error = None, e
                self.at = self.clock()
            if self.error:
                raise self.error
            return self.value


def _str(v):
    return None if v is None else str(v)


def orderbook_payload(symbol: str, result: dict, depth: int = 10) -> dict:
    def levels(rows):
        return [{"price": _str(r.get("price")), "volume": _str(r.get("volume"))} for r in (rows or [])[:depth]]

    return {
        "symbol": symbol,
        "timestamp": result.get("timestamp"),
        "currency": result.get("currency"),
        "asks": levels(result.get("asks")),  # 낮은 가격순
        "bids": levels(result.get("bids")),  # 높은 가격순
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


class LiveServer:
    def __init__(
        self,
        toss,
        symbol: str,
        *,
        host: str = "0.0.0.0",
        port: int = 8765,
        dashboard_url: str | None = None,
        page: Path = PAGE,
        orderbook_ttl: float = 1.0,
        dashboard_ttl: float = 30.0,
        opener: urllib.request.OpenerDirector | None = None,
        clock=time.monotonic,
    ):
        self.symbol = symbol
        self.page = page
        self.dashboard_url = dashboard_url
        self._opener = opener or urllib.request.build_opener()
        self.orderbook = _Cache(lambda: orderbook_payload(symbol, toss.orderbook(symbol)), orderbook_ttl, clock)
        self.dashboard = _Cache(self._fetch_dashboard, dashboard_ttl, clock)
        self.httpd = ThreadingHTTPServer((host, port), self._handler())
        self.httpd.daemon_threads = True

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def _fetch_dashboard(self) -> bytes:
        url = f"{self.dashboard_url}?t={int(time.time())}"
        with self._opener.open(urllib.request.Request(url, headers={"Cache-Control": "no-cache"}), timeout=10) as r:
            return r.read()

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # 초당 요청이 들어오니 bot.log 에 남기지 않음
                log.debug("live %s - %s", self.address_string(), fmt % args)

            def _send(self, status: int, body: bytes, ctype: str):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, obj):
                self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    self._send(200, server.page.read_bytes(), "text/html; charset=utf-8")
                elif path == "/api/orderbook":
                    try:
                        self._json(200, server.orderbook.get())
                    except Exception as e:
                        self._json(502, {"error": str(e)})
                elif path == "/dashboard.json":
                    if not server.dashboard_url:
                        self._json(404, {"error": "config.toml 에 [dashboard] 설정이 없습니다"})
                        return
                    try:
                        self._send(200, server.dashboard.get(), "application/json; charset=utf-8")
                    except Exception as e:
                        self._json(502, {"error": str(e)})
                else:
                    self._json(404, {"error": "not found"})

        return Handler

    def start(self) -> "LiveServer":
        threading.Thread(target=self.httpd.serve_forever, name="live-server", daemon=True).start()
        return self

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
