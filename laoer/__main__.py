"""CLI: python -m laoer {run|plan|order|report|status|notify-test|kakao-login}"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.parse
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .bot import Bot
from .config import Config, ConfigError, load_config
from .dashboard import Dashboard, GitHubPublisher
from .notify import (
    DiscordNotifier,
    KakaoNotifier,
    KakaoTokenStore,
    Message,
    Notifier,
    NotifyError,
    kakao_authorize_url,
    kakao_exchange_code,
)
from .state import State
from .toss import TossClient, TossError

log = logging.getLogger("laoer")


def setup_logging(state_dir: Path, verbose: bool) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    file = RotatingFileHandler(state_dir / "bot.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(fmt)
    root.addHandler(file)


def kakao_token_path(cfg: Config) -> Path:
    return cfg.run.state_dir / "kakao_token.json"


def build_notifier(cfg: Config) -> Notifier:
    channels = []
    if cfg.discord:
        channels.append(DiscordNotifier(cfg.discord.webhook_url, username=cfg.discord.username))
    if cfg.kakao:
        channels.append(
            KakaoNotifier(
                cfg.kakao.rest_api_key,
                kakao_token_path(cfg),
                client_secret=cfg.kakao.client_secret,
                link_url=cfg.kakao.link_url,
            )
        )
    if not channels:
        log.warning("알림 채널이 설정되지 않았습니다 — 콘솔/로그에만 남습니다.")
    return Notifier(channels)


def build_bot(cfg: Config) -> Bot:
    toss = TossClient(
        cfg.toss.client_id,
        cfg.toss.client_secret,
        base_url=cfg.toss.base_url,
        token_path=cfg.run.state_dir / "toss_token.json",
        account_seq=cfg.toss.account_seq,
    )
    bot = Bot(cfg, toss, build_notifier(cfg), State(cfg.run.state_dir / "state.json"))
    if cfg.dashboard:
        d = cfg.dashboard
        bot.dashboard = Dashboard(GitHubPublisher(d.github_token, d.repo, d.branch), d.password, d.heartbeat_minutes)
    return bot


def cmd_kakao_login(cfg: Config) -> int:
    if not cfg.kakao:
        print("config.toml 의 [notify.kakao] rest_api_key 를 먼저 채우세요.")
        return 2
    print("1) 아래 주소를 브라우저에서 열어 카카오 로그인 후 '카카오톡 메시지 전송'에 동의하세요.\n")
    print("   " + kakao_authorize_url(cfg.kakao.rest_api_key, cfg.kakao.redirect_uri) + "\n")
    print(f"2) {cfg.kakao.redirect_uri} 로 이동하면(페이지가 안 열려도 괜찮음) 주소창 URL 전체를 복사해 붙여넣으세요.")
    raw = input("URL 또는 code: ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query).get("code", [raw])[0] if "code=" in raw else raw
    try:
        tok = kakao_exchange_code(cfg.kakao.rest_api_key, cfg.kakao.redirect_uri, code, client_secret=cfg.kakao.client_secret)
        KakaoTokenStore(kakao_token_path(cfg)).save(tok)
        KakaoNotifier(
            cfg.kakao.rest_api_key, kakao_token_path(cfg), client_secret=cfg.kakao.client_secret, link_url=cfg.kakao.link_url
        ).send(Message("✅ 카카오톡 알림 연결 완료", ["앞으로 무매봇 알림이 '나와의 채팅'으로 옵니다."]))
    except NotifyError as e:
        print(f"실패: {e}")
        return 1
    print(f"완료! 토큰 저장 위치: {kakao_token_path(cfg)}")
    return 0


def latest_day(bot: Bot) -> str | None:
    days = [d for d, v in bot.state.days.items() if v.get("status") in ("placed", "dry_run", "alert", "placing")]
    return max(days) if days else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m laoer", description="라오어 무한매수법 TECL 봇 (토스증권 Open API)")
    ap.add_argument("-c", "--config", default="config.toml", help="설정 파일 경로 (기본: config.toml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="상시 실행: 매 정규장 주문 → 마감 후 체결 알림")
    p.add_argument("--quiet", action="store_true", help="시작 알림을 보내지 않음 (절전 해제 시 재시작용)")
    sub.add_parser("plan", help="다음 정규장에 낼 주문을 계산만 해서 보여줌 (주문/상태 변경 없음)")
    p = sub.add_parser("order", help="다음(진행 중) 정규장 주문을 지금 바로 냄")
    p.add_argument("--force", action="store_true", help="이미 낸 날이어도 봇 주문을 취소하고 다시 냄")
    p = sub.add_parser("report", help="체결 결과를 지금 확인해 알림")
    p.add_argument("--date", help="미국 영업일 YYYY-MM-DD (기본: 가장 최근 주문일)")
    p = sub.add_parser("status", help="보유/T값/사이클 상태")
    p.add_argument("--notify", action="store_true", help="상태를 알림으로도 보냄")
    sub.add_parser("dashboard", help="모니터링 페이지 데이터를 지금 갱신")
    sub.add_parser("notify-test", help="디스코드/카톡 테스트 메시지")
    sub.add_parser("kakao-login", help="카카오 '나에게 보내기' 토큰 발급")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        cfg = load_config(args.config, require_toss=args.cmd not in ("notify-test", "kakao-login"))
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg.run.state_dir, args.verbose)

    if args.cmd == "kakao-login":
        return cmd_kakao_login(cfg)
    if args.cmd == "notify-test":
        notifier = build_notifier(cfg)
        notifier.send(Message("🔔 무매봇 알림 테스트", [f"{cfg.symbol} 봇 알림이 정상적으로 연결됐습니다."], "success"))
        return 0 if notifier.channels else 1

    bot = build_bot(cfg)
    try:
        if args.cmd == "run":
            bot.run_forever(quiet=args.quiet)
        elif args.cmd == "plan":
            print(bot.preview().text())
        elif args.cmd == "status":
            msg = bot.status()
            print(msg.text())
            if args.notify:
                bot.notifier.send(msg)
        elif args.cmd == "order":
            s = bot.next_session()
            if not s:
                print("예정된 미국 정규장이 없습니다.")
                return 1
            if bot.clock() >= bot.cutoff_time(s):
                print(f"정규장 마감 {cfg.run.order_cutoff_minutes}분 전이 지나 주문하지 않습니다.")
                return 1
            prev = bot.state.days.get(s.date)
            if prev and not args.force:
                print(f"{s.date} 은 이미 처리됨 ({prev.get('status')}). 다시 내려면 --force")
                return 1
            print(json.dumps(bot.place(s, force=args.force), ensure_ascii=False, indent=1))
        elif args.cmd == "dashboard":
            if not bot.dashboard:
                print("config.toml 의 [dashboard] github_token / password 를 먼저 채우세요.")
                return 1
            bot.dashboard.publish(bot, "manual")
            owner, repo = cfg.dashboard.repo.split("/", 1)
            print(f"갱신 완료 → https://{owner}.github.io/{repo}/")
        elif args.cmd == "report":
            date = args.date or latest_day(bot)
            if not date or date not in bot.state.days:
                print("리포트할 주문 기록이 없습니다.")
                return 1
            bot.report(date, final=True)
    except TossError as e:
        print(f"토스증권 API 오류: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
