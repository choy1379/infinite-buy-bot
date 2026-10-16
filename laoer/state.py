"""봇 상태(JSON) — 사이클 정보, 날짜별 주문 기록, 완료 사이클 이력."""

from __future__ import annotations

import json
import os
from pathlib import Path

KEEP_DAYS = 120


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._stamp = None
        self._load()

    def _load(self) -> None:
        self.data: dict = {"version": 1, "cycle": None, "days": {}, "history": []}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            self._stamp = self._file_stamp()

    def _file_stamp(self):
        st = self.path.stat()
        return st.st_mtime_ns, st.st_size

    def reload_if_changed(self) -> bool:
        """다른 프로세스(`python -m laoer order --force` 등)가 파일을 바꿨으면 다시 읽는다. 안 그러면 다음 저장 때 그 기록을 덮어쓴다."""
        if not self.path.exists() or self._file_stamp() == self._stamp:
            return False
        self._load()
        return True

    @property
    def cycle(self) -> dict | None:
        return self.data.get("cycle")

    @cycle.setter
    def cycle(self, value: dict | None) -> None:
        self.data["cycle"] = value

    @property
    def days(self) -> dict:
        return self.data.setdefault("days", {})

    @property
    def history(self) -> list:
        return self.data.setdefault("history", [])

    def next_cycle_id(self) -> int:
        ids = [c.get("id", 0) for c in self.history]
        if self.cycle:
            ids.append(self.cycle.get("id", 0))
        return max(ids, default=0) + 1

    def save(self) -> None:
        days = self.days
        for old in sorted(days)[:-KEEP_DAYS]:
            del days[old]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        self._stamp = self._file_stamp()
