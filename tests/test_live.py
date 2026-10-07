"""실시간 호가 페이지 서버: 경로, 캐시, 오류 처리."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from laoer.live import LiveServer, _Cache, orderbook_payload

BOOK = {
    "timestamp": "2026-10-07T14:00:00Z",
    "currency": "USD",
    "asks": [{"price": "101.30", "volume": "120"}],
    "bids": [{"price": "101.25", "volume": "50"}],
}


class FakeToss:
    def __init__(self):
        self.calls = 0
        self.fail = None

    def orderbook(self, symbol):
        self.calls += 1
        if self.fail:
            raise self.fail
        return BOOK


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class CacheTest(unittest.TestCase):
    def test_reuses_value_and_error_within_ttl(self):
        clock, n = Clock(), []

        def fetch():
            n.append(1)
            if len(n) == 2:
                raise RuntimeError("boom")
            return len(n)

        c = _Cache(fetch, 1.0, clock)
        self.assertEqual((c.get(), c.get()), (1, 1))
        clock.t = 1.0
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                c.get()
        clock.t = 2.0
        self.assertEqual(c.get(), 3)
        self.assertEqual(len(n), 3)


class PayloadTest(unittest.TestCase):
    def test_depth_and_strings(self):
        rows = [{"price": 100 + i, "volume": i} for i in range(15)]
        p = orderbook_payload("TECL", {"asks": rows, "bids": rows[:3]}, depth=10)
        self.assertEqual(len(p["asks"]), 10)
        self.assertEqual(p["bids"][0], {"price": "100", "volume": "0"})
        self.assertEqual(p["symbol"], "TECL")


class FakeMarket:
    def __init__(self):
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return {"rows": [{"id": "wti", "name": "WTI 원유", "price": 90.55}]}


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.toss = FakeToss()
        self.clock = Clock()
        self.market = FakeMarket()
        self.srv = LiveServer(self.toss, "TECL", host="127.0.0.1", port=0, clock=self.clock, market=self.market).start()
        self.base = f"http://127.0.0.1:{self.srv.port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.srv.close()

    def get(self, path):
        try:
            with self.opener.open(self.base + path, timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_page(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"book-fold", body)

    def test_orderbook_is_cached_for_many_viewers(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.get("/api/orderbook?t=1"))) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(all(r[0] == 200 for r in results))
        self.assertEqual(json.loads(results[0][2])["asks"][0]["price"], "101.30")
        self.assertEqual(self.toss.calls, 1)
        self.clock.t = 1.5
        self.get("/api/orderbook")
        self.assertEqual(self.toss.calls, 2)

    def test_orderbook_allows_github_pages_page(self):
        _, headers, _ = self.get("/api/orderbook")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        req = urllib.request.Request(self.base + "/api/orderbook", method="OPTIONS")
        with self.opener.open(req, timeout=5) as r:
            self.assertEqual(r.status, 204)
            self.assertEqual(r.headers["Access-Control-Allow-Private-Network"], "true")
        _, page_headers, _ = self.get("/")
        self.assertIsNone(page_headers["Access-Control-Allow-Origin"])

    def test_orderbook_error_is_502(self):
        self.toss.fail = RuntimeError("403 IP")
        status, _, body = self.get("/api/orderbook")
        self.assertEqual(status, 502)
        self.assertIn("403 IP", json.loads(body)["error"])

    def test_market_cached_with_cors(self):
        status, headers, body = self.get("/api/market")
        self.get("/api/market")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(json.loads(body)["rows"][0]["price"], 90.55)
        self.assertEqual(self.market.calls, 1)
        self.clock.t = 10.0
        self.get("/api/market")
        self.assertEqual(self.market.calls, 2)

    def test_dashboard_without_config_and_unknown_path(self):
        self.assertEqual(self.get("/dashboard.json")[0], 404)
        self.assertEqual(self.get("/state.json")[0], 404)


if __name__ == "__main__":
    unittest.main()
