"""config.toml 로딩. 비밀값은 환경변수로도 줄 수 있다 (파일 값이 비어 있을 때)."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .strategy import Params
from .toss import DEFAULT_BASE_URL


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class TossConfig:
    client_id: str
    client_secret: str
    account_seq: int | None
    base_url: str


@dataclass(frozen=True)
class RunConfig:
    alert_only: bool  # mode = "alert": 주문 없이 오늘 낼 주문만 알려줌 (직접 주문용)
    dry_run: bool
    order_offset_minutes: int
    order_cutoff_minutes: int
    report_offset_minutes: int
    report_not_before_kst: tuple[int, int] | None
    skip_if_open_orders: bool
    state_dir: Path


@dataclass(frozen=True)
class DiscordConfig:
    webhook_url: str
    username: str


@dataclass(frozen=True)
class KakaoConfig:
    rest_api_key: str
    client_secret: str
    redirect_uri: str
    link_url: str


@dataclass(frozen=True)
class DashboardConfig:
    github_token: str
    repo: str
    branch: str
    password: str
    heartbeat_minutes: int


@dataclass(frozen=True)
class LiveConfig:
    host: str
    port: int  # 0 = 끔


@dataclass(frozen=True)
class Config:
    toss: TossConfig
    symbol: str
    capital_usd: Decimal
    compound: bool
    params: Params
    run: RunConfig
    discord: DiscordConfig | None
    kakao: KakaoConfig | None
    dashboard: DashboardConfig | None = None
    live: LiveConfig = LiveConfig("0.0.0.0", 8765)


def _val(section: dict, key: str, env: str | None = None, default=None):
    v = section.get(key)
    if (v is None or v == "") and env:
        v = os.environ.get(env) or None
    return default if v is None or v == "" else v


def _dec(v, name: str) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception as e:
        raise ConfigError(f"{name} 값이 숫자가 아닙니다: {v!r}") from e


def _hhmm(v) -> tuple[int, int] | None:
    if not v:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(v))
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ConfigError(f"report_not_before_kst 는 HH:MM 형식이어야 합니다: {v!r}")
    return int(m[1]), int(m[2])


def load_config(path: str | Path, *, require_toss: bool = True) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"설정 파일이 없습니다: {path} — config.example.toml 을 복사해서 만드세요.")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    base_dir = path.resolve().parent

    t = raw.get("toss", {})
    toss = TossConfig(
        client_id=str(_val(t, "client_id", "TOSS_CLIENT_ID", "")),
        client_secret=str(_val(t, "client_secret", "TOSS_CLIENT_SECRET", "")),
        account_seq=int(t["account_seq"]) if t.get("account_seq") else None,
        base_url=str(t.get("base_url") or DEFAULT_BASE_URL),
    )
    if require_toss and (not toss.client_id or not toss.client_secret):
        raise ConfigError("[toss] client_id / client_secret 이 필요합니다 (또는 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET).")

    s = raw.get("strategy", {})
    symbol = str(s.get("symbol", "TECL")).upper()
    if not re.fullmatch(r"[A-Z0-9.\-]+", symbol):
        raise ConfigError(f"symbol 형식이 잘못됐습니다: {symbol!r}")
    capital = _dec(s.get("capital_usd", 0), "capital_usd")
    if capital <= 0:
        raise ConfigError("[strategy] capital_usd(1사이클 원금, 달러)를 0보다 크게 설정하세요.")
    splits = int(s.get("splits", 20))
    if splits < 2:
        raise ConfigError("splits 는 2 이상이어야 합니다.")
    params = Params(
        splits=splits,
        target_pct=_dec(s.get("target_pct", 15), "target_pct"),
        star_base_pct=_dec(s["star_base_pct"], "star_base_pct") if "star_base_pct" in s else None,
        star_slope=_dec(s["star_slope"], "star_slope") if "star_slope" in s else None,
        first_buy_premium_pct=_dec(s.get("first_buy_premium_pct", 12), "first_buy_premium_pct"),
        extra_buy_levels=int(s.get("extra_buy_levels", 0)),
        quarter_sell=bool(s.get("quarter_sell", True)),
    )
    if params.target_pct <= 0:
        raise ConfigError("target_pct 는 0보다 커야 합니다.")

    r = raw.get("run", {})
    state_dir = Path(r.get("state_dir", "state"))
    mode = str(r.get("mode", "trade")).lower()
    if mode not in ("trade", "alert"):
        raise ConfigError(f'[run] mode 는 "trade" 또는 "alert" 이어야 합니다: {mode!r}')
    run = RunConfig(
        alert_only=mode == "alert",
        dry_run=bool(r.get("dry_run", True)),
        order_offset_minutes=int(r.get("order_offset_minutes", 15)),
        order_cutoff_minutes=int(r.get("order_cutoff_minutes", 20)),
        report_offset_minutes=int(r.get("report_offset_minutes", 20)),
        report_not_before_kst=_hhmm(r.get("report_not_before_kst", "")),
        skip_if_open_orders=bool(r.get("skip_if_open_orders", True)),
        state_dir=state_dir if state_dir.is_absolute() else base_dir / state_dir,
    )

    n = raw.get("notify", {})
    d = n.get("discord", {})
    webhook = str(_val(d, "webhook_url", "DISCORD_WEBHOOK_URL", ""))
    discord = DiscordConfig(webhook, str(d.get("username", f"{symbol} 무매봇"))) if webhook else None

    k = n.get("kakao", {})
    kakao_key = str(_val(k, "rest_api_key", "KAKAO_REST_API_KEY", ""))
    kakao = (
        KakaoConfig(
            rest_api_key=kakao_key,
            client_secret=str(_val(k, "client_secret", "KAKAO_CLIENT_SECRET", "")),
            redirect_uri=str(k.get("redirect_uri", "https://localhost")),
            link_url=str(k.get("link_url", "https://tossinvest.com")),
        )
        if kakao_key
        else None
    )

    db = raw.get("dashboard", {})
    gh_token = str(_val(db, "github_token", "TECL_DASHBOARD_TOKEN", ""))
    dashboard = None
    if gh_token:
        password = str(_val(db, "password", "TECL_DASHBOARD_PASSWORD", ""))
        if len(password) < 8:
            raise ConfigError("[dashboard] password 는 8자 이상으로 정하세요 (공개 페이지의 금액 정보를 잠그는 비밀번호).")
        dashboard = DashboardConfig(
            github_token=gh_token,
            repo=str(db.get("repo", "choy1379/infinite-buy-bot")),
            branch=str(db.get("branch", "tecl-data")),
            password=password,
            heartbeat_minutes=int(db.get("heartbeat_minutes", 180)),
        )

    lv = raw.get("live", {})
    try:
        live = LiveConfig(host=str(lv.get("host", "0.0.0.0")), port=int(lv.get("port", 8765)))
    except (TypeError, ValueError) as e:
        raise ConfigError(f"[live] port 는 숫자여야 합니다: {e}") from e
    if not 0 <= live.port <= 65535:
        raise ConfigError("[live] port 는 0~65535 (0 = 끔)")

    return Config(
        toss=toss,
        symbol=symbol,
        capital_usd=capital,
        compound=bool(s.get("compound", False)),
        params=params,
        run=run,
        discord=discord,
        kakao=kakao,
        dashboard=dashboard,
        live=live,
    )
