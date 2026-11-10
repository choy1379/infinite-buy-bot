"""매일 종가 저장 (GitHub Actions 가 장 마감 뒤 실행, market-data 브랜치에 커밋).

폴더 안 종목별 csv(date,close)에 새 날짜 종가만 이어 붙인다. 파일이 없으면 상장 이후 전체를 받는다.
야후 종가는 분할 반영, 배당 미반영. 봇은 이 파일을 쓰지 않는다. 사용법: python -m laoer.closes 폴더
"""

from __future__ import annotations

import sys
from datetime import datetime, time
from pathlib import Path

from laoer.altprices import NY, fetch

SYMBOLS = ("XLK", "TECL", "SOXL", "SOXX", "TQQQ", "QQQ")


def last_date(path: Path) -> str | None:
    lines = path.read_text(encoding="utf-8").split() if path.exists() else []
    return lines[-1].split(",")[0] if len(lines) > 1 else None


def new_rows(closes: dict[str, float], after: str | None, now: datetime) -> list[str]:
    """after 보다 뒤 날짜만. 오늘 것은 마감(16:30 뉴욕) 뒤에만 — 장중 값이 종가로 굳지 않게."""
    today = now.astimezone(NY)
    cut = today.date().isoformat() if today.time() < time(16, 30) else "9999"
    return [f"{d},{c}" for d, c in sorted(closes.items()) if (after is None or d > after) and d < cut]


def update(folder: Path, symbol: str, now: datetime, fetcher=fetch) -> int:
    path = folder / f"{symbol}.csv"
    after = last_date(path)
    rows = new_rows(fetcher(symbol, rng="1mo" if after else "max"), after, now)
    if rows:
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(("" if after else "date,close\n") + "\n".join(rows) + "\n")
    return len(rows)


def main(argv: list[str]) -> int:
    folder = Path(argv[0])
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now(NY)
    print({s: update(folder, s, now) for s in SYMBOLS})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
