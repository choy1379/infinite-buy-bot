"""Cloudflare 임시 터널: cloudflared 출력에서 주소를 찾아 알리고, 끊기면 비운다."""

import io
import unittest

from laoer.tunnel import QuickTunnel

LOG = """2026-10-08T00:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-10-08T00:00:01Z INF +--------------------------------------------------------------------------------------------+
2026-10-08T00:00:01Z INF |  https://brave-otter-sample-words.trycloudflare.com                                        |
2026-10-08T00:00:02Z INF Registered tunnel connection connIndex=0
"""


class FakeProc:
    def __init__(self, text):
        self.stderr = io.StringIO(text)

    def wait(self):
        return 0

    def poll(self):
        return 0


class TunnelTest(unittest.TestCase):
    def test_reports_url_then_clears_when_cloudflared_exits(self):
        seen, cmds = [], []

        def popen(cmd, **kw):
            cmds.append(cmd)
            return FakeProc(LOG)

        t = QuickTunnel(8765, seen.append, popen=popen)
        t.run_once()
        self.assertEqual(seen, ["https://brave-otter-sample-words.trycloudflare.com", None])
        self.assertEqual(cmds[0][-2:], ["--url", "http://localhost:8765"])

    def test_missing_cloudflared_stops_quietly(self):
        def popen(cmd, **kw):
            raise FileNotFoundError(cmd[0])

        t = QuickTunnel(8765, lambda u: None, popen=popen, sleep=lambda s: self.fail("should not retry"))
        t.loop()  # 바로 끝남

    def test_restarts_with_backoff_after_crash(self):
        calls, sleeps = [], []

        def popen(cmd, **kw):
            calls.append(1)
            if len(calls) == 3:
                t.stopped = True
            return FakeProc("")

        t = QuickTunnel(8765, lambda u: None, popen=popen, sleep=sleeps.append)
        t.loop()
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [20.0, 40.0])

    def test_callback_error_does_not_break_tunnel(self):
        def boom(url):
            raise RuntimeError("github down")

        t = QuickTunnel(8765, boom, popen=lambda cmd, **kw: FakeProc(LOG))
        t.run_once()
        self.assertIsNone(t.url)


if __name__ == "__main__":
    unittest.main()
