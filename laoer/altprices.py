"""대시보드 '다른 종목이었다면' 비교용 일봉 종가 (GitHub Actions 가 장중·장 마감 후 실행).

야후 일봉을 받아 {"updatedAt", "closes": {종목: {뉴욕 날짜: 종가}}} 형태의 alt.json 을 쓴다.
봇은 이 파일을 쓰지 않는다 (대시보드 화면만 읽음). 사용법: python -m laoer.altprices 출력파일
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SYMBOLS = ("TECL", "SOXL", "TQQQ")
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=1y&interval=1d"
NY = ZoneInfo("America/New_York")


def parse(chart: dict) -> dict[str, float]:
    """야후 chart 응답 → {날짜: 종가}. 장중이면 마지막 날은 현재가. 빈 값(휴장 등)은 건너뜀."""
    r = chart["chart"]["result"][0]
    closes = r["indicators"]["quote"][0]["close"]
    out = {}
    for ts, c in zip(r.get("timestamp") or [], closes):
        if c is not None:
            out[datetime.fromtimestamp(ts, NY).date().isoformat()] = round(c, 4)
    m = r.get("meta") or {}
    if m.get("regularMarketPrice") and m.get("regularMarketTime"):   # 야후가 마지막 날 일봉을 늦게 채울 때가 있음 → 그날 종가는 meta 값으로
        out.setdefault(datetime.fromtimestamp(m["regularMarketTime"], NY).date().isoformat(), round(m["regularMarketPrice"], 4))
    return out


def fetch(symbol: str, opener=None, timeout: float = 20) -> dict[str, float]:
    req = urllib.request.Request(URL.format(symbol), headers={"User-Agent": "Mozilla/5.0"})
    with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as r:
        return parse(json.load(r))


def main(argv: list[str]) -> int:
    closes = {s: fetch(s) for s in SYMBOLS}
    if not all(closes.values()):
        print("빈 시세가 있어 올리지 않음", {s: len(v) for s, v in closes.items()})
        return 1
    body = {"updatedAt": datetime.now(timezone.utc).isoformat(), "closes": closes}
    with open(argv[0], "w", encoding="utf-8") as f:
        json.dump(body, f, separators=(",", ":"))
    print({s: max(v) for s, v in closes.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
