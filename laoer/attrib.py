"""SOXL vs TECL 하루 차이 분해: 각 기초지수 상위 10종목의 비중 × 등락률(기여도)."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .market import UA
from .notify import Message

log = logging.getLogger(__name__)

# SOXL = ICE 반도체 지수(SOXX와 같은 지수) 3배, TECL = Technology Select Sector 지수(XLK와 같은 지수) 3배
SOXX_CSV = ("https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/1467271812596.ajax"
            "?fileType=csv&fileName=SOXX_holdings&dataType=fund&siteEntryPassthrough=true")  # passthrough: 투자자 유형 선택 HTML 건너뜀
XLK_XLSX = "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xlk.xlsx"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=5d&interval=1d"

# shortcut: 발행사 비중 파일을 못 받을 때만 쓰는 대략 비중(%, 2026-11 추정), 자주 틀리면 갱신
FALLBACK = {
    "SOXL": {"NVDA": 8, "AVGO": 8, "AMD": 8, "MU": 8, "TSM": 7, "INTC": 4, "QCOM": 4, "TXN": 4, "AMAT": 4, "LRCX": 4},
    "TECL": {"NVDA": 15, "AAPL": 13, "MSFT": 12, "AVGO": 5, "ORCL": 3, "PLTR": 3, "AMD": 3, "MU": 3, "CSCO": 2, "IBM": 2},
}
NAMES = {"NVDA": "엔비디아", "AAPL": "애플", "MSFT": "MS", "AVGO": "브로드컴", "AMD": "AMD", "MU": "마이크론",
         "TSM": "TSMC", "INTC": "인텔", "QCOM": "퀄컴", "TXN": "TI", "AMAT": "어플라이드", "LRCX": "램리서치",
         "KLAC": "KLA", "ADI": "ADI", "MRVL": "마벨", "ASML": "ASML", "ORCL": "오라클", "PLTR": "팔란티어",
         "CSCO": "시스코", "IBM": "IBM", "CRM": "세일즈포스", "ADBE": "어도비", "NOW": "서비스나우", "INTU": "인튜이트"}


def _name(t: str) -> str:
    return NAMES.get(t, t)


def parse_soxx_csv(text: str, n: int = 10) -> dict[str, float]:
    """iShares 보유종목 CSV: 앞쪽 설명 줄을 건너뛰고 'Ticker' 헤더부터 읽는다."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.lstrip('\ufeff"').startswith("Ticker")), None)
    if start is None:
        raise ValueError(f"CSV가 아님 (Ticker 헤더 없음, 앞부분: {text[:60]!r})")
    out = {}
    for row in csv.DictReader(lines[start:]):
        t, w = (row.get("Ticker") or "").strip(), (row.get("Weight (%)") or "").replace(",", "")
        if row.get("Asset Class", "Equity") == "Equity" and t and t != "-":
            try:
                out[t] = float(w)
            except ValueError:
                continue
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:n])


def _xlsx_rows(data: bytes) -> list[list[str]]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows = []
    for r in sheet.iter(f"{{{ns['m']}}}row"):
        row = []
        for c in r.findall("m:c", ns):
            col = sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(re.sub(r"\d", "", c.get("r", "A")))))
            row += [""] * (col - 1 - len(row))
            v = c.find("m:v", ns)
            is_ = c.find("m:is", ns)
            if c.get("t") == "s" and v is not None:
                row.append(shared[int(v.text)])
            elif is_ is not None:
                row.append("".join(t.text or "" for t in is_.iter(f"{{{ns['m']}}}t")))
            else:
                row.append(v.text if v is not None else "")
        rows.append(row)
    return rows


def parse_xlk_xlsx(data: bytes, n: int = 10) -> dict[str, float]:
    """SSGA 보유종목 xlsx: 'Ticker'·'Weight' 헤더 줄 아래가 종목."""
    rows = _xlsx_rows(data)
    h = next(i for i, r in enumerate(rows) if "Ticker" in r and "Weight" in r)
    ti, wi = rows[h].index("Ticker"), rows[h].index("Weight")
    out = {}
    for r in rows[h + 1:]:
        if len(r) > max(ti, wi) and r[ti].strip():
            try:
                out[r[ti].strip()] = float(r[wi])
            except ValueError:
                continue
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:n])


def day_change(body: dict) -> tuple[str, float]:
    """야후 일봉 5일치에서 마지막 거래일과 전일 대비 등락률(%)."""
    res = body["chart"]["result"][0]
    pts = [(t, c) for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c is not None]
    (_, prev), (ts, last) = pts[-2], pts[-1]
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"), (last / prev - 1) * 100


def analyze(weights: dict[str, dict[str, float]], moves: dict[str, float]) -> dict:
    """weights: {"SOXL": {티커: 비중%}, ...}, moves: {티커: 등락%} (SOXL·TECL 자체 포함)."""
    out = {}
    for etf, ws in weights.items():
        rows = [(t, w, moves[t], w * moves[t] / 100) for t, w in ws.items() if t in moves]
        index = moves[etf] / 3
        out[etf] = {"etf": moves[etf], "index": index, "rows": rows, "rest": index - sum(r[3] for r in rows)}
    contrib = {etf: {t: c for t, _, _, c in v["rows"]} for etf, v in out.items()}
    gap = {t: contrib["TECL"].get(t, 0) - contrib["SOXL"].get(t, 0) for t in set(contrib["SOXL"]) | set(contrib["TECL"])}
    out["gap"] = sorted(gap.items(), key=lambda kv: -abs(kv[1]))[:3]
    return out


def message(day: str, a: dict, moves: dict[str, float], source: str) -> Message:
    s, t = a["SOXL"], a["TECL"]
    lines = [f"기초지수(÷3) 반도체 {s['index']:+.1f}% · 기술주 {t['index']:+.1f}%", "", "차이를 만든 종목 (TECL 기여 − SOXL 기여)"]
    for tk, g in a["gap"]:
        lines.append(f"· {_name(tk)} {moves[tk]:+.1f}% → {g:+.2f}%p")
    for etf in ("SOXL", "TECL"):
        v = a[etf]
        top = sorted(v["rows"], key=lambda r: r[3])
        lines += ["", f"{etf} top10 (비중 · 등락 · 기여)"]
        lines += [f"{_name(tk)} {w:.0f}% · {m:+.1f}% · {c:+.2f}%p" for tk, w, m, c in top]
        lines.append(f"나머지 종목 {v['rest']:+.2f}%p")
    lines += ["", f"비중: {source}"]
    return Message(f"📊 {day[5:].replace('-', '/')} SOXL {s['etf']:+.1f}% · TECL {t['etf']:+.1f}% 차이", lines)


class Attrib:
    def __init__(self, opener=None, timeout: float = 15.0):
        self.opener = opener or urllib.request.build_opener()
        self.timeout = timeout

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with self.opener.open(req, timeout=self.timeout) as r:
            return r.read()

    def weights(self) -> tuple[dict[str, dict[str, float]], str]:
        """각 ETF 따로 받고, 실패한 쪽만 고정 비중으로 바꾼다."""
        srcs = {"SOXL": ("iShares SOXX", lambda: parse_soxx_csv(self._get(SOXX_CSV).decode("utf-8-sig"))),
                "TECL": ("SSGA XLK", lambda: parse_xlk_xlsx(self._get(XLK_XLSX)))}
        out, notes = {}, []
        for etf, (label, fetch) in srcs.items():
            try:
                out[etf] = fetch()
                if not out[etf]:
                    raise ValueError("종목 0개")
                notes.append(f"{label} 당일 파일")
            except Exception as e:  # 발행사 파일 형식/접속이 바뀌어도 알림은 보낸다
                log.warning("%s 비중 받기 실패, 고정 비중 사용: %s", label, e)
                out[etf] = FALLBACK[etf]
                notes.append(f"{etf} 고정 추정값({label} 받기 실패)")
        return out, " · ".join(notes)

    def build(self) -> Message:
        weights, source = self.weights()
        moves, day = {}, ""
        for tk in {"SOXL", "TECL"} | set(weights["SOXL"]) | set(weights["TECL"]):
            try:
                d, moves[tk] = day_change(json.loads(self._get(CHART.format(t=tk))))
                if tk == "SOXL":
                    day = d
            except Exception as e:  # 한 종목 실패는 '나머지'로 흡수
                log.warning("%s 일봉 실패: %s", tk, e)
        if "SOXL" not in moves or "TECL" not in moves:
            raise ValueError("SOXL/TECL 일봉을 못 받았어요")
        return message(day, analyze(weights, moves), moves, source)
