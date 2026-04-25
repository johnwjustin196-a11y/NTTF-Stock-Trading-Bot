"""Centralized logging setup."""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_config, project_root

_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        _setup_root()
        _configured = True
    return logging.getLogger(name)


def _setup_root() -> None:
    cfg = load_config()
    level = getattr(logging, cfg.get("logging", {}).get("level", "INFO").upper(), logging.INFO)
    log_file = os.environ.get("BOT_LOG_FILE") or cfg.get("logging", {}).get("file", "logs/bot.log")

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # stdout — uses console_level so backtest/live runs don't flood the terminal
    console_level_str = cfg.get("logging", {}).get("console_level", "INFO").upper()
    console_level = getattr(logging, console_level_str, logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(console_level)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # rotating file
    log_path = project_root() / log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding='utf-8')
    fh.setFormatter(fmt)
    root.addHandler(fh)
