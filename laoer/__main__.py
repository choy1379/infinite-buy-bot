"""CLI: python -m laoer {run|live|market|plan|order|report|status|dashboard|notify-test|kakao-login}"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.parse
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .bot import Bot
from .config import Config, ConfigError, load_config
from .dashboard import Dashboard, GitHubPublisher, live_account_fetcher
from .live import LiveServer
from .market import Market
from .tunnel import QuickTunnel
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
    bot.fx_rate = lambda: Market().quote("yahoo", "KRW=X")["price"]
    if cfg.dashboard:
        d = cfg.dashboard
        bot.dashboard = Dashboard(GitHubPublisher(d.github_token, d.repo, d.branch), d.password, d.heartbeat_minutes)
    return bot


def start_live(cfg: Config, bot: Bot, *, port: int | None = None, serve: bool = True) -> LiveServer | None:
    port = cfg.live.port if port is None else port
    if not port:
        return None
    dash = cfg.dashboard
    url = f"https://raw.githubusercontent.com/{dash.repo}/{dash.branch}/dashboard.json" if dash else None
    try:
        account = live_account_fetcher(bot.toss, dash.password) if dash else None
        srv = LiveServer(bot.toss, cfg.symbol, host=cfg.live.host, port=port, dashboard_url=url, account=account)
    except OSError as e:  # 포트가 이미 쓰이는 중 등 — 봇은 그대로 돈다
        log.warning("실시간 호가 페이지를 못 띄웠습니다 (포트 %s): %s", port, e)
        return None
    if serve:  # `live` 명령은 직접 serve_forever() 하므로 serve=False
        srv.start()
    log.info("실시간 호가 페이지: http://localhost:%s/ (같은 와이파이의 폰은 http://<이 PC IP>:%s/)", srv.port, srv.port)
    if cfg.live.tunnel:
        def on_url(url):
            bot.live_url = url
            bot._publish("tunnel")  # 새 주소를 대시보드(잠금 칸)에 올려 GitHub 페이지가 찾게 함

        QuickTunnel(srv.port, on_url).start()
    return srv


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


def cmd_market() -> int:
    rows = Market().snapshot()["rows"]
    for r in rows:
        if "error" in r:
            print(f"✗ {r['name']}: {r['error']}")
        else:
            print(f"✓ {r['name']}: {r['price']} ({r.get('change')}, {r.get('pct')}%) @ {r.get('at')}")
    return 0 if all("error" not in r for r in rows) else 1


def cmd_realized(bot: Bot, days: int, raw: bool) -> int:
    from datetime import datetime, timedelta

    from .realized import KST, RealizedLog, sync

    if raw:
        start = (datetime.now(KST) - timedelta(days=days)).date().isoformat()
        for o in bot.toss.closed_orders(start=start):
            ex = o.get("execution") or {}
            print(
                f"{o.get('orderedAt', '')[:19]}  {o.get('symbol'):<6} {o.get('side'):<4} {o.get('status'):<9} "
                f"주문 {o.get('quantity')}@{o.get('price')}  체결 {ex.get('filledQuantity')}@{ex.get('averageFilledPrice')} "
                f"수수료 {ex.get('commission')} 세금 {ex.get('tax')}  {o.get('orderId')}"
            )
        return 0
    rlog = RealizedLog(bot.cfg.run.state_dir / "realized.json")
    new = sync(bot.toss, rlog, skip_symbol=bot.symbol, days=days)
    print(f"새로 기록한 매도 {len(new)}건")
    view = rlog.view()
    for r in view["items"]:
        pl_usd = f"${r['plUsd']}" if r.get("plUsd") is not None else "–"
        pl_krw = f"{int(Decimal(r['plKrw'])):,}원" if r.get("plKrw") is not None else "–"
        print(f"  {r['date']} {r['symbol']} {r['qty']}주 매도 {r['price']} (평단 {r['cost']}) → 손익 {pl_usd} / {pl_krw} (환율 {r['fx']})")
    tu = f"${view['totalUsd']}" if view["totalUsd"] is not None else "–"
    tk = f"{int(Decimal(view['totalKrw'])):,}원" if view["totalKrw"] is not None else "–"
    print(f"합계: {tu} / {tk}")
    return 0


def cmd_tax(bot: Bot) -> int:
    from .tax import TaxLog, refresh

    r = refresh(bot.toss, TaxLog(bot.cfg.run.state_dir / "tax.json"))
    won = lambda v: f"{int(Decimal(v)):,}원"
    print(f"{r['year']}년 해외주식 양도차익(대략): {won(r['gainKrw'])}")
    for sym, v in r["bySymbol"].items():
        print(f"  {sym}: {won(v)}")
    print(f"기본공제 {won(r['deductionKrw'])} → 과세 {won(r['taxableKrw'])} → 예상 양도세 {won(r['taxKrw'])} (22%)")
    if r["unknown"]:
        print("평단을 몰라 뺀 매도(올해 이전에 산 물량):", ", ".join(f"{s} {n}건" for s, n in r["unknown"].items()))
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
    p = sub.add_parser("live", help="실시간 호가 페이지만 띄움 (run 은 자동으로 같이 띄움)")
    p.add_argument("--port", type=int, help="포트 (기본: config [live] port, 8765)")
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
    sub.add_parser("market", help="'시장' 탭 선물 시세가 받아지는지 확인 (설정 불필요)")
    p = sub.add_parser("realized", help="봇 밖에서 판 종목의 실현 손익을 기록하고 보여줌")
    p.add_argument("--days", type=int, default=14, help="최근 며칠의 종료 주문을 볼지 (기본 14)")
    p.add_argument("--raw", action="store_true", help="기록하지 않고 종료된 주문을 그대로 나열 (토스 앱 주문이 목록에 나오는지 확인용)")
    sub.add_parser("tax", help="올해 해외주식 양도세 대략치를 계산해서 보여줌")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.cmd == "market":
        return cmd_market()

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
            start_live(cfg, bot)
            bot.run_forever(quiet=args.quiet)
        elif args.cmd == "live":
            srv = start_live(cfg, bot, port=args.port or cfg.live.port or 8765, serve=False)
            if not srv:
                return 1
            print(f"실시간 호가 페이지: http://localhost:{srv.port}/  (Ctrl+C 로 종료)")
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
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
        elif args.cmd == "realized":
            return cmd_realized(bot, args.days, args.raw)
        elif args.cmd == "tax":
            return cmd_tax(bot)
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
