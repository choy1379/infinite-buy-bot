"""실시간 호가 페이지 서버: 경로, 캐시, 오류 처리."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from laoer.dashboard import decrypt, live_account_fetcher
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


class FakeAccount:
    def __init__(self):
        self.calls = 0

    def holdings_all(self):
        self.calls += 1
        return {"items": [{"symbol": "KO", "name": "코카콜라", "currency": "USD", "quantity": "434", "lastPrice": "85.98"}]}

    def buying_power(self, cur):
        return "3270.55"


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.toss = FakeToss()
        self.clock = Clock()
        self.market = FakeMarket()
        self.acct = FakeAccount()
        self.srv = LiveServer(
            self.toss, "TECL", host="127.0.0.1", port=0, clock=self.clock, market=self.market,
            account=live_account_fetcher(self.acct, "pw-12345678"),
        ).start()
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

    def test_account_is_encrypted_cached_with_cors(self):
        status, headers, body = self.get("/api/account")
        self.get("/api/account")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertNotIn(b"434", body)  # 보유 수량이 평문으로 나가면 안 됨
        self.assertNotIn("코카콜라".encode(), body)
        got = json.loads(decrypt("pw-12345678", json.loads(body)))
        self.assertEqual(got["cash"], "3270.55")
        self.assertEqual(got["account"][0]["symbol"], "KO")
        self.assertEqual(got["account"][0]["qty"], "434")
        with self.assertRaises(ValueError):
            decrypt("wrong-password", json.loads(body))
        self.assertEqual(self.acct.calls, 1)  # 30초 캐시: 여러 기기가 봐도 토스 호출은 한 번
        self.clock.t = 30.0
        self.get("/api/account")
        self.assertEqual(self.acct.calls, 2)

    def test_account_route_absent_without_password(self):
        srv = LiveServer(self.toss, "TECL", host="127.0.0.1", port=0, clock=self.clock, market=self.market).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.opener.open(f"http://127.0.0.1:{srv.port}/api/account", timeout=5)
            self.assertEqual(cm.exception.code, 404)
        finally:
            srv.close()

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


class StartLiveTest(unittest.TestCase):
    """봇이 `run` 으로 돌 때 호가 서버가 실제로 떠서 요청에 답하는지 (예전엔 만들기만 하고 시작을 안 했다)."""

    def make(self):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            symbol="TECL", dashboard=None,
            live=SimpleNamespace(host="127.0.0.1", port=0, tunnel=False),
        )
        return cfg, SimpleNamespace(toss=FakeToss())

    def test_run_path_serves_requests(self):
        from laoer.__main__ import start_live

        cfg, bot = self.make()
        srv = start_live(cfg, bot, port=_free_port())
        self.addCleanup(srv.close)
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/api/orderbook", timeout=5) as r:
            self.assertEqual(r.status, 200)

    def test_live_command_path_does_not_start_thread(self):
        from laoer.__main__ import start_live

        cfg, bot = self.make()
        srv = start_live(cfg, bot, port=_free_port(), serve=False)
        self.addCleanup(srv.httpd.server_close)
        self.assertNotIn("live-server", [t.name for t in threading.enumerate()])


def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
