import json
import re
import shutil
import subprocess
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from laoer.dashboard import Dashboard, GitHubPublisher, build_payload, decrypt, encrypt
from tests import test_bot as tb

PAGE = Path(__file__).resolve().parents[1] / "index.html"


class CryptoTest(unittest.TestCase):
    def test_roundtrip_and_wrong_password(self):
        box = encrypt("correct horse", "평단 $101.23 · 보유 37주".encode(), iterations=1000)
        self.assertEqual(decrypt("correct horse", box).decode(), "평단 $101.23 · 보유 37주")
        with self.assertRaises(ValueError):
            decrypt("wrong", box)

    @unittest.skipUnless(shutil.which("node") and PAGE.exists(), "node 또는 index.html 없음")
    def test_page_javascript_decrypts_python_output(self):
        secret = {"holding": {"qty": "37", "avg": "101.2345"}, "note": "한글 " * 40}
        box = encrypt("pw-12345678", json.dumps(secret, ensure_ascii=False).encode(), iterations=2000)
        html = PAGE.read_text(encoding="utf-8")
        src = re.search(r"(const b64 = .*?\n  async function decrypt.*?\n  }\n)", html, re.S).group(1)
        script = src + f"""
console.log(JSON.stringify(await decrypt({json.dumps('pw-12345678')}, {json.dumps(box)})));
await decrypt('nope', {json.dumps(box)}).then(() => console.log('BAD'), e => console.log('REJECTED ' + e.message));
"""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "t.mjs"
            f.write_text(script, encoding="utf-8")
            run = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = run.stdout.splitlines()
        self.assertEqual(json.loads(out[0]), secret)
        self.assertEqual(out[1], "REJECTED bad-password")


class PayloadTest(unittest.TestCase):
    DATES = tb.BotFlowTest.DATES
    setUp = tb.BotFlowTest.setUp
    tearDown = tb.BotFlowTest.tearDown
    make_bot = tb.BotFlowTest.make_bot
    at = tb.BotFlowTest.at

    def test_public_part_has_no_amounts(self):
        self.broker.qty, self.broker.avg = D(37), D("101.2345")
        self.broker.cash = D("6250.11")
        bot = self.make_bot()
        self.at(2026, 10, 6, 22, 45)
        bot.tick()
        h = self.broker.holding("TECL")
        payload = build_payload(bot, holding=h, price=D("98.40"), cash=self.broker.cash, session=bot.next_session(), password="pw-12345678")
        public = json.dumps({k: v for k, v in payload.items() if k != "secret"})
        for leak in ("101.2345", "6250.11", "98.4", "3745", "10000"):
            self.assertNotIn(leak, public)
        self.assertEqual(payload["position"]["t"], "7.50")
        self.assertEqual([o["leg"] for o in payload["days"][0]["orders"]], ["avg", "star", "quarter", "target"])
        secret = json.loads(decrypt("pw-12345678", payload["secret"]))
        self.assertEqual(secret["holding"]["avg"], "101.2345")
        self.assertEqual(secret["cash"], "6250.11")
        self.assertEqual(secret["days"][0]["orders"][0]["price"], "101.23")

    def test_next_plan_prices_only_in_secret(self):
        self.broker.qty, self.broker.avg = D(37), D("101.2345")
        bot = self.make_bot()
        s = bot.next_session()
        plan, _, _ = bot.make_plan(s.date, mutate=False)
        payload = build_payload(bot, holding=self.broker.holding("TECL"), session=s, plan=plan, password="pw-12345678")
        nxt = payload["nextPlan"]
        self.assertEqual((nxt["date"], nxt["phase"]), ("2026-10-06", "first_half"))
        self.assertEqual([o["leg"] for o in nxt["orders"]], ["avg", "star", "quarter", "target"])
        self.assertNotIn("101.23", json.dumps(nxt))
        sec = json.loads(decrypt("pw-12345678", payload["secret"]))["nextPlan"]
        self.assertEqual(sec["orders"][0], {"leg": "avg", "price": "101.23", "qty": plan.orders[0].qty})
        self.assertEqual(bot.state.days, {})  # 미리 계산만, 상태는 그대로

    def test_close_context_only_in_secret(self):
        bot = self.make_bot()
        bot.state.days["2026-10-06"] = {"date": "2026-10-06", "status": "placed", "orders": [], "close": "251.50",
                                        "prev_close": "248.26", "fx": "1383.20", "pnl": "2.27", "daily_pct": "0.45"}
        payload = build_payload(bot, password="pw-12345678")
        self.assertNotIn("248.26", json.dumps({k: v for k, v in payload.items() if k != "secret"}))
        day = json.loads(decrypt("pw-12345678", payload["secret"]))["days"][0]
        self.assertEqual((day["prevClose"], day["fx"]), ("248.26", "1383.20"))

    def test_tunnel_url_only_in_secret(self):
        bot = self.make_bot()
        bot.live_url = "https://brave-otter.trycloudflare.com"
        payload = build_payload(bot, password="pw-12345678")
        self.assertNotIn("trycloudflare", json.dumps({k: v for k, v in payload.items() if k != "secret"}))
        self.assertEqual(json.loads(decrypt("pw-12345678", payload["secret"]))["liveUrl"], bot.live_url)

    def test_account_holdings_only_in_secret(self):
        bot = self.make_bot()
        raw = {"items": [{"symbol": "KO", "name": "코카콜라", "marketCountry": "US", "currency": "USD", "quantity": "120",
                          "averagePurchasePrice": "72.40", "lastPrice": "66.85",
                          "marketValue": {"purchaseAmount": "8688.00", "amount": "8022.00"},
                          "profitLoss": {"amount": "-666.00", "rate": "-0.0767"}}]}
        payload = build_payload(bot, password="pw-12345678", account=raw)
        self.assertNotIn("8022", json.dumps({k: v for k, v in payload.items() if k != "secret"}))
        acct = json.loads(decrypt("pw-12345678", payload["secret"]))["account"]
        self.assertEqual(acct[0]["symbol"], "KO")
        self.assertEqual((acct[0]["value"], acct[0]["plRate"], acct[0]["dayRate"]), ("8022.00", "-0.0767", None))
        self.assertIsNone(json.loads(decrypt("pw-12345678", build_payload(bot, password="pw-12345678")["secret"]))["account"])

    def test_realized_losses_only_in_secret(self):
        bot = self.make_bot()
        realized = {"items": [{"symbol": "KO", "plUsd": "-90.68", "plKrw": "-125908"}], "totalUsd": "-90.68", "totalKrw": "-125908"}
        payload = build_payload(bot, password="pw-12345678", realized=realized)
        self.assertNotIn("125890", json.dumps({k: v for k, v in payload.items() if k != "secret"}))
        self.assertEqual(json.loads(decrypt("pw-12345678", payload["secret"]))["realized"]["totalKrw"], "-125908")
        self.assertIsNone(json.loads(decrypt("pw-12345678", build_payload(bot, password="pw-12345678")["secret"]))["realized"])

    def test_no_password_means_no_secret(self):
        bot = self.make_bot()
        self.assertIsNone(build_payload(bot)["secret"])


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.branch_exists = False

    def open(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        url = req.full_url.replace("https://api.github.com/repos/o/r", "")
        self.calls.append((req.get_method(), url, body))
        m = req.get_method()
        if m == "GET" and url == "/git/ref/heads/tecl-data":
            return self._resp(200, {"object": {"sha": "head1"}}) if self.branch_exists else self._resp(404, {"message": "Not Found"})
        if m == "GET" and url == "/git/commits/head1":
            return self._resp(200, {"tree": {"sha": "basetree"}})
        if m == "POST" and url == "/git/trees":
            return self._resp(201, {"sha": "tree1"})
        if m == "POST" and url == "/git/commits":
            return self._resp(201, {"sha": f"commit{len(self.calls)}"})
        if m == "PATCH" and url == "/git/refs/heads/tecl-data":
            return self._resp(200, {}) if self.branch_exists else self._resp(422, {"message": "Reference does not exist"})
        if m == "POST" and url == "/git/refs":
            self.branch_exists = True
            return self._resp(201, {})
        return self._resp(500, {"message": "unexpected"})

    @staticmethod
    def _resp(status, payload):
        import io
        import urllib.error

        raw = json.dumps(payload).encode()
        if status >= 400:
            raise urllib.error.HTTPError("u", status, "x", {}, io.BytesIO(raw))

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        r = R(raw)
        r.status = status
        return r


class PublisherTest(unittest.TestCase):
    def test_creates_branch_then_replaces_with_parentless_commit(self):
        gh = FakeGitHub()
        pub = GitHubPublisher("tok", "o/r", "tecl-data", opener=gh)
        pub.put('{"a":"한글"}'.encode(), "first")
        tree = [c for c in gh.calls if c[1] == "/git/trees"][0][2]["tree"][0]
        self.assertEqual((tree["path"], tree["content"]), ("dashboard.json", '{"a":"한글"}'))
        commit = [c for c in gh.calls if c[1] == "/git/commits"][0][2]
        self.assertEqual((commit["tree"], commit["parents"]), ("tree1", []))
        self.assertIn(("POST", "/git/refs", {"ref": "refs/heads/tecl-data", "sha": "commit2"}), gh.calls)
        gh.calls.clear()
        pub.put(b'{"a":2}', "second")
        self.assertEqual([c[0] for c in gh.calls], ["POST", "POST", "PATCH"])
        self.assertTrue(gh.calls[-1][2]["force"])

    def test_put_can_carry_extra_files(self):
        gh = FakeGitHub()
        pub = GitHubPublisher("tok", "o/r", "tecl-data", opener=gh)
        pub.put(b"{}", "m", {"alive.json": b'{"x":1}'})
        tree = [c for c in gh.calls if c[1] == "/git/trees"][0][2]["tree"]
        self.assertEqual([t["path"] for t in tree], ["dashboard.json", "alive.json"])

    def test_touch_changes_one_file_on_top_of_existing_tree(self):
        gh = FakeGitHub()
        pub = GitHubPublisher("tok", "o/r", "tecl-data", opener=gh)
        with self.assertRaises(RuntimeError):  # 브랜치가 없으면 대시보드가 먼저
            pub.touch("alive.json", b"{}", "alive")
        gh.branch_exists = True
        gh.calls.clear()
        pub.touch("alive.json", b'{"a":1}', "alive")
        body = [c for c in gh.calls if c[1] == "/git/trees"][0][2]
        self.assertEqual(body["base_tree"], "basetree")
        self.assertEqual([t["path"] for t in body["tree"]], ["alive.json"])
        self.assertEqual([c for c in gh.calls if c[1] == "/git/commits"][0][2]["parents"], [])
        self.assertTrue(gh.calls[-1][2]["force"])


class FakePublisher:
    def __init__(self):
        self.puts = []
        self.touches = []
        self.extras = []

    def put(self, content, message, extra=None):
        self.puts.append(message)
        self.extras.append(extra)

    def touch(self, path, content, message):
        self.touches.append((path, json.loads(content)))


class AccountRefreshTest(unittest.TestCase):
    DATES = tb.BotFlowTest.DATES
    setUp = tb.BotFlowTest.setUp
    tearDown = tb.BotFlowTest.tearDown
    make_bot = tb.BotFlowTest.make_bot
    at = tb.BotFlowTest.at

    def test_publishes_only_when_account_changes(self):
        bot = self.make_bot()
        pub = FakePublisher()
        dash = bot.dashboard = Dashboard(pub, "pw-12345678", heartbeat_minutes=180, account_minutes=5)
        items = [{"symbol": "TECL", "quantity": "10", "lastPrice": "100", "marketValue": {"amount": "1000"}}]
        self.broker.holdings_all = lambda: {"items": [dict(i) for i in items]}
        self.at(2026, 10, 7, 10, 0)
        self.assertTrue(dash.account_due(bot.clock()))
        dash.publish(bot, "start")
        self.assertFalse(dash.account_due(bot.clock()))
        self.at(2026, 10, 7, 10, 5)
        self.assertTrue(dash.account_due(bot.clock()))
        dash.refresh_account(bot)  # 그대로면 안 올림
        self.assertEqual(pub.puts, ["dashboard: start"])
        self.assertFalse(dash.account_due(bot.clock()))
        items[0]["lastPrice"], items[0]["marketValue"] = "101", {"amount": "1010"}
        self.at(2026, 10, 7, 10, 10)
        dash.refresh_account(bot)
        self.assertEqual(pub.puts, ["dashboard: start", "dashboard: account"])

    def test_publishes_again_when_tax_refresh_finishes(self):
        bot = self.make_bot()
        pub = FakePublisher()
        dash = bot.dashboard = Dashboard(pub, "pw-12345678", account_minutes=5)
        self.broker.holdings_all = lambda: {"items": []}
        self.at(2026, 10, 7, 10, 0)
        dash.publish(bot, "start")
        # 뒤에서 돌던 양도세·체결 목록 계산이 끝나 tax.json 이 새로 저장됨
        bot.cfg.run.state_dir.mkdir(parents=True, exist_ok=True)
        (bot.cfg.run.state_dir / "tax.json").write_text(json.dumps({"fx": {}, "result": {"fills": []}, "at": "2026-10-07T10:01:00+09:00"}), encoding="utf-8")
        self.at(2026, 10, 7, 10, 5)
        dash.refresh_account(bot)
        self.assertEqual(pub.puts, ["dashboard: start", "dashboard: account"])

    def test_alive_beat_every_five_minutes_and_rides_along_with_publish(self):
        bot = self.make_bot()
        bot.live_url = "https://abc.trycloudflare.com"
        pub = FakePublisher()
        dash = Dashboard(pub, "pw-12345678", alive_minutes=5)
        self.at(2026, 10, 7, 10, 0)
        self.assertTrue(dash.alive_due(bot.clock()))
        dash.publish(bot, "start")  # 본문을 올릴 때 살아있음 파일도 같이
        alive = json.loads(pub.extras[-1]["alive.json"])
        self.assertEqual(alive["symbol"], bot.symbol)
        self.assertEqual(decrypt("pw-12345678", alive["tunnel"]).decode(), "https://abc.trycloudflare.com")
        self.assertFalse(dash.alive_due(bot.clock()))
        self.at(2026, 10, 7, 10, 3)
        self.assertFalse(dash.alive_due(bot.clock()))
        self.at(2026, 10, 7, 10, 4)  # 5분 대기가 모자라게 끝나도 거르지 않는다 (1분 여유)
        self.assertTrue(dash.alive_due(bot.clock()))
        self.at(2026, 10, 7, 10, 5)
        dash.beat(bot)
        self.assertEqual([t[0] for t in pub.touches], ["alive.json"])
        self.assertTrue(pub.touches[0][1]["aliveAt"].startswith("2026-10-07T10:05"))
        self.assertFalse(dash.alive_due(bot.clock()))

    def test_alive_off_when_zero_and_failure_waits_for_next_interval(self):
        bot = self.make_bot()
        pub = FakePublisher()
        dash = Dashboard(pub, "pw-12345678", alive_minutes=0)
        self.assertFalse(dash.alive_due(bot.clock()))
        dash.beat(bot)
        dash.publish(bot, "x")
        self.assertEqual((pub.touches, pub.extras[-1]), ([], None))
        dash = Dashboard(pub, "pw-12345678", alive_minutes=5)
        pub.touch = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        self.at(2026, 10, 7, 10, 0)
        with self.assertRaises(RuntimeError):
            dash.beat(bot)
        self.assertFalse(dash.alive_due(bot.clock()))

    def test_zero_turns_it_off(self):
        dash = Dashboard(FakePublisher(), "pw-12345678", account_minutes=0)
        self.assertFalse(dash.account_due(tb.datetime(2026, 10, 7, tzinfo=tb.KST)))


if __name__ == "__main__":
    unittest.main()
