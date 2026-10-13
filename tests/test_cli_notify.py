"""`python -m laoer notify` : 설정된 알림 채널로 문구를 그대로 보낸다 (토스 키 없이도 동작, 웹훅 주소는 출력 안 함)."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from laoer import __main__ as cli
from laoer.notify import Notifier

CONFIG = """
[strategy]
symbol = "TECL"
capital_usd = 1000
[notify.discord]
webhook_url = "https://discord.example/api/webhooks/SECRET-TOKEN"
"""


class Channel:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class NotifyCommandTest(unittest.TestCase):
    def test_discord_sender_name_is_claude_for_notify_only(self):
        seen = []
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_text(CONFIG, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                for argv in (["notify", "hi"],):
                    with mock.patch.object(cli, "DiscordNotifier", side_effect=lambda url, username, **kw: seen.append(username) or Channel()):
                        cli.main(["--config", str(cfg), *argv])
        self.assertEqual(seen, ["Claude"])
        from laoer.config import load_config

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_text(CONFIG, encoding="utf-8")
            with mock.patch.object(cli, "DiscordNotifier", side_effect=lambda url, username, **kw: seen.append(username) or Channel()):
                cli.build_notifier(load_config(cfg, require_toss=False))
        self.assertNotEqual(seen[-1], "Claude")  # 봇 자동 알림은 설정의 이름(기본 TECL 무매봇) 그대로

    def run_cli(self, argv, channels):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.toml"
            cfg.write_text(CONFIG, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cli, "build_notifier", return_value=Notifier(channels)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--config", str(cfg), *argv])
        return code, out.getvalue() + err.getvalue()

    def test_sends_text_as_title_and_lines(self):
        ch = Channel()
        code, output = self.run_cli(["notify", "승인 필요:", "compound 변경", "--level", "warn"], [ch])
        self.assertEqual(code, 0)
        self.assertEqual((ch.sent[0].title, ch.sent[0].lines, ch.sent[0].level), ("[Claude] 승인 필요: compound 변경", [], "warn"))
        self.assertNotIn("SECRET-TOKEN", output)

    def test_multiline_text_and_length_cap(self):
        ch = Channel()
        self.run_cli(["notify", "제목\n둘째 줄\n" + "x" * 5000], [ch])
        self.assertEqual((ch.sent[0].title, ch.sent[0].lines[0]), ("[Claude] 제목", "둘째 줄"))
        self.assertLessEqual(len(ch.sent[0].text()), 1520)  # 본문 1500자 + 머리말

    def test_exit_code_1_without_any_channel(self):
        code, _ = self.run_cli(["notify", "안녕"], [])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
