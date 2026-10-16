import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

from laoer import watchdog
from laoer.dashboard import encrypt

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)
PW = "pw-12345678"


class FakeOpener:
    def __init__(self, routes):
        self.routes, self.sent = routes, []

    def open(self, req, timeout=None):
        url = req.full_url
        if req.get_method() == "POST":
            self.sent.append(json.loads(req.data))
            return self._ok(b"")
        r = self.routes.get(url)
        if isinstance(r, int):
            raise urllib.error.HTTPError(url, r, "x", {}, io.BytesIO(b""))
        if r is None:
            raise urllib.error.URLError("down")
        return self._ok(r)

    @staticmethod
    def _ok(raw):
        class R(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R(raw)


def alive_json(minutes_ago, tunnel="https://t.example"):
    at = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return json.dumps({"aliveAt": at, "tunnel": encrypt(PW, tunnel.encode(), iterations=1000) if tunnel else None}).encode()


class WatchdogTest(unittest.TestCase):
    ENV = {"DISCORD_WEBHOOK_URL": "https://discord.test/hook", "ALIVE_URL": "https://a.test/alive.json", "DASHBOARD_PASSWORD": PW}

    def run_main(self, routes, env=None):
        op = FakeOpener(routes)
        code = watchdog.main({**self.ENV, **(env or {})}, opener=op, now=NOW)
        return code, op.sent

    def test_healthy_sends_nothing(self):
        code, sent = self.run_main({"https://a.test/alive.json": alive_json(7), "https://t.example/api/market": b"ok"})
        self.assertEqual((code, sent), (0, []))

    def test_stale_alerts_as_claude(self):
        code, sent = self.run_main({"https://a.test/alive.json": alive_json(55), "https://t.example/api/market": b"ok"})
        self.assertEqual(code, 1)
        self.assertEqual(sent[0]["username"], "Claude")
        self.assertIn("55분", json.dumps(sent[0], ensure_ascii=False))

    def test_tunnel_down_alerts_but_4xx_is_alive(self):
        code, sent = self.run_main({"https://a.test/alive.json": alive_json(5), "https://t.example/api/market": 530})
        self.assertEqual(code, 1)
        self.assertEqual(self.run_main({"https://a.test/alive.json": alive_json(5), "https://t.example/api/market": 403})[0], 0)

    def test_threshold_depends_on_korean_night(self):
        day = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)  # 한국 12:00
        night = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)  # 한국 23:00
        for now, minutes_ago, want in ((day, 30, 0), (day, 50, 1), (night, 10, 0), (night, 20, 1)):
            at = (now - timedelta(minutes=minutes_ago)).isoformat()
            routes = {"https://a.test/alive.json": json.dumps({"aliveAt": at, "tunnel": None}).encode()}
            code = watchdog.main({**self.ENV, "DASHBOARD_PASSWORD": ""}, opener=FakeOpener(routes), now=now)
            self.assertEqual(code, want, (now, minutes_ago))

    def test_falls_back_to_dashboard_json_when_alive_file_missing(self):
        env = {k: v for k, v in self.ENV.items() if k != "ALIVE_URL"}
        base = "https://raw.githubusercontent.com/choy1379/infinite-buy-bot/tecl-data/"
        def run(minutes_ago):
            routes = {base + "alive.json": 404, base + "dashboard.json": json.dumps({"updatedAt": (NOW - timedelta(minutes=minutes_ago)).isoformat()}).encode()}
            return watchdog.main(env, opener=FakeOpener(routes), now=NOW)  # NOW 는 한국 낮
        self.assertEqual((run(200), run(300)), (0, 1))

    def test_without_password_skips_tunnel(self):
        code, _ = self.run_main({"https://a.test/alive.json": alive_json(5)}, {"DASHBOARD_PASSWORD": ""})
        self.assertEqual(code, 0)

    def test_wrong_password_and_missing_file_are_reported(self):
        self.assertEqual(self.run_main({"https://a.test/alive.json": alive_json(5)}, {"DASHBOARD_PASSWORD": "other"})[0], 1)
        self.assertEqual(self.run_main({"https://a.test/alive.json": 404})[0], 1)

    def test_missing_webhook_and_test_message(self):
        self.assertEqual(watchdog.main({}, now=NOW), 2)
        code, sent = self.run_main({"https://a.test/alive.json": alive_json(5), "https://t.example/api/market": b"ok"}, {"SEND_TEST": "1"})
        self.assertEqual((code, len(sent)), (0, 1))


if __name__ == "__main__":
    unittest.main()
