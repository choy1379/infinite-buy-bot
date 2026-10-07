"""'시장' 탭 시세: 코스피200 선물(야간 포함), WTI, 미국 지수 선물.

브라우저(GitHub Pages)는 CORS 때문에 네이버/야후를 직접 못 부르니 봇 PC가 대신 받아 /api/market 으로 넘긴다.
둘 다 공개 시세이고 비공식 API라 형식이 바뀔 수 있다. 종목마다 따로 받아서 하나가 실패해도 나머지는 보인다.
  python -m laoer market   -> 지금 받아지는지 콘솔에서 확인
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# (id, 이름, 소스, 코드). 네이버는 주소 후보를 차례로 시도
SYMBOLS = [
    ("k200f", "코스피200 선물", "naver", "FUT"),
    ("wti", "WTI 원유", "yahoo", "CL=F"),
    ("es", "S&P500 E-mini", "yahoo", "ES=F"),
    ("nq", "나스닥100 E-mini", "yahoo", "NQ=F"),
]
NAVER_URLS = [
    "https://m.stock.naver.com/api/index/{code}/basic",
    "https://polling.finance.naver.com/api/realtime/domestic/index/{code}",
]
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{code}?range=1d&interval=5m"


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("+", ""))
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(KST).isoformat(timespec="seconds")


def _naver_time(v) -> str | None:
    if not v:
        return None
    s = str(v)
    if s.isdigit() and len(s) >= 12:  # 202610072240(00)
        dt = datetime.strptime(s[:14].ljust(14, "0"), "%Y%m%d%H%M%S").replace(tzinfo=KST)
        return _iso(dt)
    try:
        dt = datetime.fromisoformat(s)
        return _iso(dt if dt.tzinfo else dt.replace(tzinfo=KST))
    except ValueError:
        return None


def is_night(at: str | None) -> bool:
    """코스피200 야간선물 시간(18:00~다음날 06:00 KST)에 찍힌 시세인지."""
    if not at:
        return False
    h = datetime.fromisoformat(at).astimezone(KST).hour
    return h >= 18 or h < 6


def parse_naver(body: dict) -> dict:
    d = body
    if isinstance(d.get("datas"), list) and d["datas"]:
        d = d["datas"][0]
    price = _num(d.get("closePrice") or d.get("nv"))
    if price is None:
        raise ValueError("네이버 응답에 가격이 없어요")
    change = _num(d.get("compareToPreviousClosePrice") or d.get("cv"))
    pct = _num(d.get("fluctuationsRatio") or d.get("cr"))
    falling = (d.get("compareToPreviousPrice") or {}).get("name") in ("FALLING", "LOWER_LIMIT")
    if falling:  # 네이버는 하락폭을 부호 없이 줄 때가 있음
        change = -abs(change) if change is not None else None
        pct = -abs(pct) if pct is not None else None
    at = _naver_time(d.get("localTradedAt") or d.get("tradedAt"))
    return {"price": price, "change": change, "pct": pct, "at": at}


def parse_yahoo(body: dict) -> dict:
    meta = body["chart"]["result"][0]["meta"]
    price = _num(meta.get("regularMarketPrice"))
    prev = _num(meta.get("previousClose") or meta.get("chartPreviousClose"))
    if price is None:
        raise ValueError("야후 응답에 가격이 없어요")
    change = round(price - prev, 6) if prev else None
    pct = round(change / prev * 100, 2) if prev else None
    t = meta.get("regularMarketTime")
    at = _iso(datetime.fromtimestamp(t, timezone.utc)) if t else None
    return {"price": price, "change": change, "pct": pct, "at": at}


class Market:
    def __init__(self, symbols=SYMBOLS, opener=None, timeout: float = 6.0):
        self.symbols = symbols
        self.opener = opener or urllib.request.build_opener()
        self.timeout = timeout

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with self.opener.open(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def quote(self, source: str, code: str) -> dict:
        if source == "yahoo":
            return parse_yahoo(self._get(YAHOO_URL.format(code=urllib.parse.quote(code))))
        err: Exception | None = None
        for u in NAVER_URLS:
            try:
                return parse_naver(self._get(u.format(code=code)))
            except Exception as e:  # 다음 주소 후보로
                err = e
        raise err or ValueError("네이버 주소가 없어요")

    def _row(self, sym) -> dict:
        sid, name, source, code = sym
        row = {"id": sid, "name": name}
        try:
            row.update(self.quote(source, code))
            if sid == "k200f" and is_night(row.get("at")):
                row["name"] = "코스피200 야간선물"
        except Exception as e:
            log.debug("시세 %s 실패: %s", name, e)
            row["error"] = str(e) or type(e).__name__
        return row

    def snapshot(self) -> dict:
        with ThreadPoolExecutor(max_workers=len(self.symbols)) as ex:
            rows = list(ex.map(self._row, self.symbols))
        return {"rows": rows, "fetchedAt": _iso(datetime.now(timezone.utc))}
