import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from laoer import __main__ as cli


class NotifyCommandTest(unittest.TestCase):
    def cfg(self):
        return SimpleNamespace(discord=SimpleNamespace(webhook_url="https://discord.example/hook", username="봇"))

    def test_sends_title_and_lines_to_discord(self):
        sent = []

        def fake_post(opener, url, data, headers, timeout=15.0):
            sent.append(json.loads(data))
            return 204, ""

        with mock.patch("laoer.notify._post", fake_post), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_notify(self.cfg(), "허락 필요\\n서버-배포 스레드에 써 주세요"), 0)
        embed = sent[0]["embeds"][0]
        self.assertEqual(embed["title"], "허락 필요")
        self.assertEqual(embed["description"], "서버-배포 스레드에 써 주세요")

    def test_failure_returns_1_without_url(self):
        err = io.StringIO()
        with mock.patch("laoer.notify._post", lambda *a, **k: (400, "bad")), redirect_stderr(err):
            self.assertEqual(cli.cmd_notify(self.cfg(), "x"), 1)
        self.assertNotIn("discord.example", err.getvalue())

    def test_no_webhook(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_notify(SimpleNamespace(discord=None), "x"), 1)


if __name__ == "__main__":
    unittest.main()
