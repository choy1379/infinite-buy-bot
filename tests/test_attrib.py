"""SOXL vs TECL 차이 분해: 발행사 파일 해석, 일봉 등락, 기여도."""

import io
import unittest
import zipfile

from laoer.attrib import FALLBACK, Attrib, analyze, day_change, message, parse_holdings_csv, parse_xlk_xlsx

CSV = '''Direxion Shares ETF Trust
Direxion Daily Semiconductor Bull 3X Shares
Holdings Data as of: 11/04/2026

TradeDate,AccountTicker,StockTicker,SecurityDescription,Shares,Price,MarketValue,Money,HoldingsPercent
11/04/2026,SOXL,NVDA,NVIDIA CORP,100,1,600,0,6.00
11/04/2026,SOXL,MU,MICRON TECHNOLOGY,100,1,400,0,4.00
11/04/2026,SOXL,,ICE SEMICONDUCTOR INDEX SWAP,0,0,9000,0,90.00
11/04/2026,SOXL,DGCXX,DREYFUS GOVT CASH MGMT FUND,1,1,10,0,0.10
'''


def xlsx(rows):
    strings, cells = [], []
    for ri, row in enumerate(rows, 1):
        cs = []
        for ci, v in enumerate(row):
            ref = f"{chr(65 + ci)}{ri}"
            if isinstance(v, str):
                strings.append(v)
                cs.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
            else:
                cs.append(f'<c r="{ref}"><v>{v}</v></c>')
        cells.append(f'<row r="{ri}">{"".join(cs)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(cells)}</sheetData></worksheet>')
    return b.getvalue()


class AttribTest(unittest.TestCase):
    def test_direxion_csv_keeps_stocks_only(self):
        self.assertEqual(parse_holdings_csv(CSV), {"NVDA": 60.0, "MU": 40.0})

    def test_xlk_xlsx(self):
        data = xlsx([["Fund Name:", "XLK"], ["Name", "Ticker", "Identifier", "Weight"],
                     ["APPLE", "AAPL", "x", 13.2], ["MICROSOFT", "MSFT", "y", 12.1], ["CASH", "", "", 0.1]])
        self.assertEqual(parse_xlk_xlsx(data), {"AAPL": 13.2, "MSFT": 12.1})

    def test_day_change_skips_missing_close(self):
        body = {"chart": {"result": [{"timestamp": [1793716200, 1793802600, 1793889000],
                                      "indicators": {"quote": [{"close": [100.0, 110.0, None]}]}}]}}
        day, pct = day_change(body)
        self.assertAlmostEqual(pct, 10.0)
        self.assertEqual(day, "2026-11-04")

    def test_gap_names_apple_when_only_tecl_holds_it(self):
        w = {"SOXL": {"NVDA": 8, "MU": 8}, "TECL": {"NVDA": 15, "AAPL": 13}}
        moves = {"SOXL": -5.0, "TECL": 0.0, "NVDA": -3.0, "MU": -7.0, "AAPL": 5.0}
        a = analyze(w, moves)
        self.assertEqual(a["gap"][0][0], "AAPL")  # TECL만 +0.65%p
        self.assertAlmostEqual(a["SOXL"]["rest"], -5 / 3 - (-0.24 - 0.56))
        text = message("2026-11-04", a, moves, "test").text()
        self.assertIn("11/04 SOXL -5.0% · TECL +0.0%", text)
        self.assertIn("애플 +5.0%", text)

    def test_only_failed_side_falls_back(self):
        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass
        class Opener:
            def open(self, req, timeout):
                return R(b"<html>not csv</html>" if "direxion" in req.full_url else data)
        data = xlsx([["Name", "Ticker", "Weight"], ["APPLE", "AAPL", 13.2]])
        w, src = Attrib(opener=Opener()).weights()
        self.assertEqual(w, {"SOXL": FALLBACK["SOXL"], "TECL": {"AAPL": 13.2}})
        self.assertEqual(src, "SOXL 고정 추정값(Direxion SOXL 받기 실패) · SSGA XLK 당일 파일")


if __name__ == "__main__":
    unittest.main()
