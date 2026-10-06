"""알림: 디스코드 웹훅 + 카카오톡 '나에게 보내기'."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"tecl-infinite-buy/{__version__}"
KAKAO_AUTH = "https://kauth.kakao.com"
KAKAO_API = "https://kapi.kakao.com"
KAKAO_TEXT_LIMIT = 200  # 카카오 텍스트 템플릿 본문 최대 길이

COLORS = {"info": 0x3B82F6, "success": 0x22C55E, "warn": 0xF59E0B, "error": 0xEF4444}


class NotifyError(Exception):
    pass


@dataclass
class Message:
    title: str
    lines: list[str] = field(default_factory=list)
    level: str = "info"  # info | success | warn | error

    def text(self) -> str:
        return "\n".join([self.title, *self.lines])


def _post(opener, url: str, data: bytes, headers: dict, timeout: float = 15.0):
    req = urllib.request.Request(url, data=data, method="POST", headers={"User-Agent": USER_AGENT, **headers})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _post_form(opener, url: str, form: dict, headers: dict | None = None):
    return _post(
        opener,
        url,
        urllib.parse.urlencode(form).encode("utf-8"),
        {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8", **(headers or {})},
    )


# --------------------------------------------------------------------- discord
class DiscordNotifier:
    name = "discord"

    def __init__(self, webhook_url: str, *, username: str = "TECL 무매봇", opener=None, sleep=time.sleep):
        self.webhook_url = webhook_url
        self.username = username
        self._opener = opener or urllib.request.build_opener()
        self._sleep = sleep

    def payload(self, msg: Message) -> dict:
        return {
            "username": self.username,
            "embeds": [
                {
                    "title": msg.title[:256],
                    "description": "\n".join(msg.lines)[:4000] or "​",
                    "color": COLORS.get(msg.level, COLORS["info"]),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ],
        }

    def send(self, msg: Message) -> None:
        data = json.dumps(self.payload(msg)).encode("utf-8")
        for _ in range(2):
            status, body = _post(self._opener, self.webhook_url, data, {"Content-Type": "application/json"})
            if status in (200, 204):
                return
            if status == 429:
                try:
                    wait = float(json.loads(body).get("retry_after", 1))
                except (ValueError, AttributeError):
                    wait = 1.0
                self._sleep(min(wait, 10))
                continue
            break
        raise NotifyError(f"discord {status}: {body[:200]}")


# ----------------------------------------------------------------------- kakao
def chunk_text(text: str, limit: int = KAKAO_TEXT_LIMIT) -> list[str]:
    """줄 단위로 limit 이하 조각으로 나눈다. 여러 조각이면 (1/3) 같은 번호를 붙인다."""

    def split(lim: int) -> list[str]:
        chunks: list[str] = []
        cur = ""
        for line in text.split("\n"):
            while len(line) > lim:
                if cur:
                    chunks.append(cur)
                    cur = ""
                chunks.append(line[:lim])
                line = line[lim:]
            candidate = f"{cur}\n{line}" if cur else line
            if len(candidate) <= lim:
                cur = candidate
            else:
                chunks.append(cur)
                cur = line
        if cur:
            chunks.append(cur)
        return chunks

    chunks = split(limit)
    if len(chunks) <= 1:
        return chunks
    chunks = split(limit - 8)
    return [f"({i}/{len(chunks)}) {c}" for i, c in enumerate(chunks, 1)]


class KakaoTokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, tok: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tok, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)


def _token_record(d: dict, now: float, prev: dict | None = None) -> dict:
    tok = dict(prev or {})
    tok["access_token"] = d["access_token"]
    tok["expires_at"] = now + int(d.get("expires_in", 21599))
    if d.get("refresh_token"):
        tok["refresh_token"] = d["refresh_token"]
        tok["refresh_expires_at"] = now + int(d.get("refresh_token_expires_in", 5183999))
    return tok


def kakao_authorize_url(rest_api_key: str, redirect_uri: str) -> str:
    q = urllib.parse.urlencode(
        {"response_type": "code", "client_id": rest_api_key, "redirect_uri": redirect_uri, "scope": "talk_message"}
    )
    return f"{KAKAO_AUTH}/oauth/authorize?{q}"


def kakao_exchange_code(rest_api_key, redirect_uri, code, *, client_secret="", opener=None, now=None) -> dict:
    form = {"grant_type": "authorization_code", "client_id": rest_api_key, "redirect_uri": redirect_uri, "code": code}
    if client_secret:
        form["client_secret"] = client_secret
    status, body = _post_form(opener or urllib.request.build_opener(), f"{KAKAO_AUTH}/oauth/token", form)
    if status != 200:
        raise NotifyError(f"카카오 토큰 발급 실패 {status}: {body[:300]}")
    return _token_record(json.loads(body), now if now is not None else time.time())


class KakaoNotifier:
    name = "kakao"

    def __init__(
        self,
        rest_api_key: str,
        token_path: Path,
        *,
        client_secret: str = "",
        link_url: str = "https://tossinvest.com",
        opener=None,
        clock=time.time,
    ):
        self.rest_api_key = rest_api_key
        self.client_secret = client_secret
        self.link_url = link_url
        self.store = KakaoTokenStore(token_path)
        self._opener = opener or urllib.request.build_opener()
        self._clock = clock

    def _refresh(self, tok: dict) -> dict:
        if not tok.get("refresh_token"):
            raise NotifyError("카카오 refresh token이 없습니다. `python -m laoer kakao-login`을 다시 실행하세요.")
        form = {"grant_type": "refresh_token", "client_id": self.rest_api_key, "refresh_token": tok["refresh_token"]}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        status, body = _post_form(self._opener, f"{KAKAO_AUTH}/oauth/token", form)
        if status != 200:
            raise NotifyError(f"카카오 토큰 갱신 실패 {status}: {body[:300]} — kakao-login을 다시 실행하세요.")
        tok = _token_record(json.loads(body), self._clock(), tok)
        self.store.save(tok)
        return tok

    def _access_token(self, *, force_refresh: bool = False) -> str:
        tok = self.store.load()
        if not tok:
            raise NotifyError("카카오 토큰이 없습니다. `python -m laoer kakao-login`을 먼저 실행하세요.")
        if force_refresh or tok.get("expires_at", 0) - 600 < self._clock():
            tok = self._refresh(tok)
        return tok["access_token"]

    def send(self, msg: Message) -> None:
        for chunk in chunk_text(msg.text()):
            self._send_text(chunk)

    def _send_text(self, text: str) -> None:
        template = {
            "object_type": "text",
            "text": text,
            "link": {"web_url": self.link_url, "mobile_web_url": self.link_url},
            "button_title": "토스증권 열기",
        }
        form = {"template_object": json.dumps(template, ensure_ascii=False)}
        url = f"{KAKAO_API}/v2/api/talk/memo/default/send"
        token = self._access_token()
        status, body = _post_form(self._opener, url, form, {"Authorization": f"Bearer {token}"})
        if status == 401:
            token = self._access_token(force_refresh=True)
            status, body = _post_form(self._opener, url, form, {"Authorization": f"Bearer {token}"})
        if status != 200:
            raise NotifyError(f"kakao {status}: {body[:200]}")


# --------------------------------------------------------------------- fan-out
class Notifier:
    def __init__(self, channels: list | None = None):
        self.channels = list(channels or [])

    def send(self, msg: Message) -> None:
        log.info("[알림] %s | %s", msg.title, " / ".join(msg.lines))
        for ch in self.channels:
            try:
                ch.send(msg)
            except Exception as e:  # 알림 실패가 매매를 멈추면 안 된다
                log.warning("%s 알림 실패: %s", getattr(ch, "name", ch), e)
