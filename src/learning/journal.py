"""Trade journal — append-only JSONL file per trading day."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..utils.config import load_config
from ..utils.logger import get_logger
from ..utils.market_time import now_eastern, today_str

log = get_logger(__name__)


def _path_for(date_str: str) -> Path:
    cfg = load_config()
    return Path(cfg["paths"]["journal_dir"]) / f"{date_str}.jsonl"


def append_entry(entry: dict[str, Any]) -> None:
    entry = {"timestamp": now_eastern().isoformat(), **entry}
    path = _path_for(today_str())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def load_today_journal() -> list[dict[str, Any]]:
    return load_journal(today_str())


def load_journal(date_str: str) -> list[dict[str, Any]]:
    path = _path_for(date_str)
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                log.warning(f"skip malformed journal line: {e}")
    return out
