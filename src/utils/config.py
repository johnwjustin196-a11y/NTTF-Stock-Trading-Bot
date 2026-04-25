"""Config loader. Reads config/settings.yaml and merges with env vars."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = ROOT / "config" / "settings.yaml"


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Load settings.yaml and overlay values from .env / process env."""
    load_dotenv(ROOT / ".env", override=False)

    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Attach secrets from env. Never write these to settings.yaml.
    cfg["secrets"] = {
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "newsapi_key": os.getenv("NEWSAPI_KEY", ""),
        # Optional Bearer token for a local OpenAI-compatible server that
        # has been configured to require auth. LM Studio's default setup
        # does NOT require this.
        "local_llm_api_key": os.getenv("LOCAL_LLM_API_KEY", ""),
    }

    # Resolve data paths relative to project root
    paths = cfg.setdefault("paths", {})
    for k, v in list(paths.items()):
        paths[k] = str(ROOT / v)

    # Ensure data dirs exist
    Path(paths["data_dir"]).mkdir(parents=True, exist_ok=True)
    Path(paths["journal_dir"]).mkdir(parents=True, exist_ok=True)

    return cfg


def project_root() -> Path:
    return ROOT
