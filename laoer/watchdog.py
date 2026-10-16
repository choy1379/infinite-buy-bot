"""PC 밖에서 봇이 멈췄는지 보는 감시 (GitHub Actions 가 하루 몇 번 실행).

tecl-data 브랜치의 alive.json(봇이 5분마다 올리는 '살아있음' 시각)이 너무 오래됐거나, 터널 주소가 응답하지 않으면
디스코드로 "봇 멈춘 듯" 알림을 보낸다. 환경변수:
  DISCORD_WEBHOOK_URL  (필수) 알림 보낼 디스코드 웹훅 — 저장소 시크릿
  DASHBOARD_PASSWORD   (선택) 있으면 alive.json 의 잠긴 터널 주소를 열어 응답까지 확인. 없으면 터널 확인은 건너뜀
  ALIVE_URL            alive.json 주소 (기본: 이 저장소의 tecl-data 브랜치)
  MAX_AGE_MIN          이 분수보다 오래되면 멈춘 것으로 본다 (기본: 한국 시각 22:00~05:30 은 15분, 그 외는 40분)
  (alive.json 이 아직 없으면(봇이 옛 버전) dashboard.json 의 updatedAt 으로 대신 보고, 기준은 야간 15분·그 외 4시간)
  SEND_TEST=1          문제가 없어도 테스트 알림 한 번 보냄 (웹훅 시크릿이 맞게 등록됐는지 확인용)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta, timezone

from laoer.dashboard import decrypt
from laoer.notify import DiscordNotifier, Message

NAME = "Claude"
AVATAR = "https://choy1379.github.io/infinite-buy-bot/assets/claude-avatar.png?v=2"
DEFAULT_URL = "https://raw.githubusercontent.com/choy1379/infinite-buy-bot/tecl-data/alive.json"
FALLBACK_URL = "https://raw.githubusercontent.com/choy1379/infinite-buy-bot/tecl-data/dashboard.json"
KST = timezone(timedelta(hours=9))


def is_night(now: datetime) -> bool:
    """한국 시각 22:00~05:30 (미국 장 시간대: 주문과 체결이 있는 때라 더 빡빡하게 본다)."""
    t = now.astimezone(KST).time()
    return t >= time(22, 0) or t < time(5, 30)


def fetch_json(url: str, opener=None, timeout: float = 20):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "laoer-watchdog"})
    with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as r:
        return json.loads(r.read())


def tunnel_up(url: str, opener=None, timeout: float = 15) -> bool:
    """터널 주소가 응답하면 True. 4xx 는 (막아 둔 것이니) 살아있는 것으로 본다. 5xx·연결 실패는 죽은 것."""
    req = urllib.request.Request(url, headers={"User-Agent": "laoer-watchdog"})
    try:
        with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


def check(env, *, now: datetime | None = None, opener=None) -> list[str]:
    """문제 목록 (없으면 빈 리스트)."""
    now = now or datetime.now(timezone.utc)
    night = is_night(now)
    max_age = float(env.get("MAX_AGE_MIN") or (15 if night else 40))
    try:
        alive = fetch_json(env.get("ALIVE_URL") or DEFAULT_URL, opener)
        at = datetime.fromisoformat(alive["aliveAt"])
    except Exception as e:
        if env.get("ALIVE_URL") or not (isinstance(e, urllib.error.HTTPError) and e.code == 404):
            return [f"살아있음 파일(alive.json)을 읽지 못했습니다: {type(e).__name__}"]
        try:  # 봇이 아직 옛 버전이라 alive.json 이 없을 때: 대시보드 갱신 시각으로 (하트비트가 3시간이라 낮엔 느슨하게)
            alive = {"tunnel": None}
            at = datetime.fromisoformat(fetch_json(FALLBACK_URL, opener)["updatedAt"])
            max_age = float(env.get("MAX_AGE_MIN") or (15 if night else 240))
        except Exception as e2:
            return [f"살아있음 파일과 대시보드 파일을 읽지 못했습니다: {type(e2).__name__}"]
    problems = []
    age = (now - at).total_seconds() / 60
    if age > max_age:
        problems.append(f"마지막 살아있음 신호가 {age:.0f}분 전입니다 (기준 {max_age:.0f}분).")
    pw, box = env.get("DASHBOARD_PASSWORD"), alive.get("tunnel")
    if pw and box:
        try:
            url = decrypt(pw, box).decode("utf-8")
        except Exception:
            problems.append("터널 주소를 열지 못했습니다 (DASHBOARD_PASSWORD 시크릿이 대시보드 비밀번호와 다른 듯).")
        else:
            if not tunnel_up(url.rstrip('/') + '/api/market', opener):
                problems.append("터널 주소가 응답하지 않습니다 (외부 접속이 안 될 수 있음).")
    return problems


def main(env=None, *, opener=None, now: datetime | None = None) -> int:
    env = os.environ if env is None else env
    hook = env.get("DISCORD_WEBHOOK_URL")
    if not hook:
        print("DISCORD_WEBHOOK_URL 시크릿이 없습니다. 저장소 Settings > Secrets and variables > Actions 에 등록하세요.")
        return 2
    notifier = DiscordNotifier(hook, username=NAME, avatar_url=AVATAR, opener=opener)
    problems = check(env, now=now, opener=opener)
    if problems:
        print("문제:", *problems, sep="\n- ")
        notifier.send(Message(f"[{NAME}] [감시] 봇 멈춘 듯합니다", [*problems, "서버 PC가 켜져 있는지, 봇 창이 떠 있는지 확인해 주세요."], "error"))
        return 1
    print("정상")
    if env.get("SEND_TEST") == "1":
        notifier.send(Message(f"[{NAME}] [감시] 테스트", ["봇이 정상이고, 이 알림이 보이면 웹훅 시크릿도 잘 등록된 겁니다."], "success"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
