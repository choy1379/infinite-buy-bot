"""TossClient 를 로컬 가짜 서버에 붙여 요청 형식/재시도/토큰 처리를 검증한다."""

import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from decimal import Decimal as D
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from laoer.toss import TossClient, TossError


class FakeToss:
    def __init__(self):
        self.calls = []
        self.tokens_issued = 0
        self.valid_token = None
        self.rate_limit_once = set()
        self.orders = {}


def make_handler(fake: FakeToss):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, payload, headers=None):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self, method):
            url = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            fake.calls.append((method, url.path, self.headers, raw, urllib.parse.parse_qs(url.query)))

            if url.path == "/oauth2/token":
                form = urllib.parse.parse_qs(raw.decode())
                if form.get("client_secret") != ["s"]:
                    return self._send(401, {"error": "invalid_client", "error_description": "bad"})
                fake.tokens_issued += 1
                fake.valid_token = f"tok{fake.tokens_issued}"
                return self._send(200, {"access_token": fake.valid_token, "token_type": "Bearer", "expires_in": 86400})

            if self.headers.get("Authorization") != f"Bearer {fake.valid_token}":
                return self._send(401, {"error": {"requestId": "r", "code": "unauthorized", "message": "토큰 만료"}})
            if url.path in fake.rate_limit_once:
                fake.rate_limit_once.discard(url.path)
                return self._send(429, {"error": {"requestId": "r", "code": "rate-limit-exceeded", "message": ""}}, {"Retry-After": "0"})

            if url.path == "/api/v1/accounts":
                return self._send(200, {"result": [{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}]})
            if self.headers.get("X-Tossinvest-Account") is None and url.path not in ("/api/v1/prices",):
                return self._send(400, {"error": {"requestId": "r", "code": "missing-account", "message": "헤더 누락"}})
            if url.path == "/api/v1/prices":
                return self._send(200, {"result": [{"symbol": "TECL", "lastPrice": "101.25", "currency": "USD"}]})
            if url.path == "/api/v1/holdings":
                return self._send(
                    200,
                    {
                        "result": {
                            "items": [
                                {
                                    "symbol": "TECL",
                                    "quantity": "20",
                                    "averagePurchasePrice": "100",
                                    "lastPrice": "101.25",
                                    "marketValue": {"purchaseAmount": "2000"},
                                    "profitLoss": {"rate": "0.0125"},
                                }
                            ]
                        }
                    },
                )
            if url.path == "/api/v1/orders" and method == "POST":
                body = json.loads(raw)
                if body["quantity"] == "0":
                    return self._send(422, {"error": {"requestId": "r9", "code": "invalid-quantity", "message": "수량 오류"}})
                oid = f"oid-{body['clientOrderId']}"
                fake.orders[oid] = body
                return self._send(200, {"result": {"orderId": oid, "clientOrderId": body["clientOrderId"]}})
            return self._send(404, {"error": {"requestId": "r", "code": "not-found", "message": url.path}})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return H


class TossClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeToss()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.fake))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def client(self, secret="s"):
        return TossClient(
            "c", secret, base_url=self.base, token_path=Path(self.tmp.name) / "tok.json", opener=self.opener, sleep=lambda s: None
        )

    def test_token_form_and_headers(self):
        c = self.client()
        self.assertEqual(c.price("TECL"), D("101.25"))
        method, path, headers, raw, _ = self.fake.calls[0]
        self.assertEqual((method, path), ("POST", "/oauth2/token"))
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(urllib.parse.parse_qs(raw.decode())["grant_type"], ["client_credentials"])

        h = c.holding("TECL")
        self.assertEqual((h.qty, h.avg), (D(20), D(100)))
        hold_call = [x for x in self.fake.calls if x[1] == "/api/v1/holdings"][0]
        self.assertEqual(hold_call[2]["X-Tossinvest-Account"], "7")
        self.assertEqual(hold_call[4]["symbol"], ["TECL"])

    def test_token_cached_across_clients(self):
        self.client().price("TECL")
        self.client().price("TECL")
        self.assertEqual(self.fake.tokens_issued, 1)

    def test_reissues_token_on_401(self):
        c = self.client()
        c.price("TECL")
        self.fake.valid_token = "rotated-elsewhere"
        self.assertEqual(c.price("TECL"), D("101.25"))
        self.assertEqual(self.fake.tokens_issued, 2)

    def test_retries_after_429(self):
        c = self.client()
        self.fake.rate_limit_once.add("/api/v1/prices")
        self.assertEqual(c.price("TECL"), D("101.25"))

    def test_place_loc_order_body(self):
        c = self.client()
        oid = c.place_order(symbol="TECL", side="BUY", tif="CLS", qty=3, price=D("108.99"), client_order_id="lab-TECL-x-star")
        body = self.fake.orders[oid]
        self.assertEqual(
            body,
            {
                "clientOrderId": "lab-TECL-x-star",
                "symbol": "TECL",
                "side": "BUY",
                "orderType": "LIMIT",
                "timeInForce": "CLS",
                "quantity": "3",
                "price": "108.99",
            },
        )

    def test_api_error_is_parsed(self):
        c = self.client()
        with self.assertRaises(TossError) as cm:
            c.place_order(symbol="TECL", side="BUY", tif="CLS", qty=0, price=D(1), client_order_id="x")
        self.assertEqual((cm.exception.status, cm.exception.code, cm.exception.request_id), (422, "invalid-quantity", "r9"))

    def test_bad_credentials(self):
        with self.assertRaises(TossError) as cm:
            self.client(secret="wrong").price("TECL")
        self.assertEqual(cm.exception.code, "invalid_client")


if __name__ == "__main__":
    unittest.main()
