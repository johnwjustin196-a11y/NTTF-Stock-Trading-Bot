"""Push data files to GitHub so the Streamlit Cloud dashboard stays fresh.

Called after each decision cycle, backtest run, and EOD. Only stages
data/ — source code changes are pushed manually by the developer.
Non-fatal: any git/network failure is logged and silently skipped.
"""
from __future__ import annotations
import subprocess
from .logger import get_logger

log = get_logger(__name__)


def push_data_to_github(tag: str) -> None:
    """Stage data/, commit if anything changed, and push to GitHub."""
    from .config import project_root
    root = str(project_root())
    try:
        subprocess.run(["git", "add", "data/"], cwd=root, capture_output=True, timeout=30)
        result = subprocess.run(
            ["git", "commit", "-m", f"data: {tag}"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        committed = result.returncode == 0
        push = subprocess.run(
            ["git", "push"], cwd=root, capture_output=True, text=True, timeout=60,
        )
        if push.returncode == 0:
            log.info(f"GitHub data push ok [{tag}] committed={committed}")
        else:
            log.warning(f"GitHub data push failed [{tag}]: {push.stderr.strip()[:200]}")
    except Exception as _e:
        log.warning(f"GitHub data push skipped [{tag}]: {_e}")
