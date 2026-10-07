"""Cloudflare 임시 터널: 봇 PC의 실시간 호가 서버(localhost:8765)에 https 주소를 붙인다.

계정·도메인·카드 없이 무료인 'quick tunnel'(https://xxxx.trycloudflare.com) 을 쓴다.
주소는 cloudflared 를 켤 때마다 바뀌므로, 새 주소가 나오면 on_url 로 알려 대시보드에 올린다.
그러면 GitHub Pages 페이지가 그 주소로 호가·시세를 받아 폰/회사 어디서든 보인다.
cloudflared 가 죽으면 잠시 뒤 다시 켠다. 봇을 끄면(stop.cmd) 자식 프로세스라 같이 꺼진다.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class QuickTunnel:
    def __init__(self, port: int, on_url, *, exe: str = "cloudflared", popen=subprocess.Popen, sleep=time.sleep):
        self.port, self.on_url, self.exe = port, on_url, exe
        self.popen, self.sleep = popen, sleep
        self.url: str | None = None
        self.proc = None
        self.stopped = False

    def command(self) -> list[str]:
        return [self.exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{self.port}"]

    def _set(self, url: str | None) -> None:
        if url == self.url:
            return
        self.url = url
        try:
            self.on_url(url)
        except Exception as e:  # 대시보드 갱신 실패가 터널을 멈추면 안 된다
            log.warning("터널 주소 알림 실패: %s", e)

    def run_once(self) -> None:
        """cloudflared 를 한 번 띄우고 끝날 때까지 출력에서 주소를 찾는다."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        self.proc = self.popen(
            self.command(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", creationflags=flags,
        )
        for line in self.proc.stderr:  # cloudflared 는 로그를 stderr 로 낸다
            m = URL_RE.search(line)
            if m:
                log.info("외부 접속 주소: %s", m.group(0))
                self._set(m.group(0))
        self.proc.wait()
        self._set(None)

    def loop(self) -> None:
        delay = 10.0
        while not self.stopped:
            started = time.monotonic()
            try:
                self.run_once()
            except FileNotFoundError:
                log.warning("cloudflared 가 없어 외부 접속 터널을 끕니다 (설치: winget install Cloudflare.cloudflared)")
                return
            except Exception as e:
                log.warning("터널 오류: %s", e)
            if self.stopped:
                return
            delay = 10.0 if time.monotonic() - started > 600 else min(delay * 2, 600.0)
            log.warning("터널이 끊겨 %.0f초 뒤 다시 켭니다", delay)
            self.sleep(delay)

    def start(self) -> "QuickTunnel":
        threading.Thread(target=self.loop, name="tunnel", daemon=True).start()
        return self

    def close(self) -> None:
        self.stopped = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
