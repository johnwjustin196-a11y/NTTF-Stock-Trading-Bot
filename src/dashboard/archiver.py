from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_archive_dir(data_dir: Path) -> Path:
    archive = data_dir / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    return archive


def _append_jsonl(src: Path, dst: Path) -> int:
    """Append valid JSON lines from *src* to *dst*. Returns count appended."""
    if not src.exists():
        return 0
    count = 0
    with src.open("r", encoding="utf-8") as fh:
        lines = fh.readlines()
    if not lines:
        return 0
    with dst.open("a", encoding="utf-8") as out:
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                json.loads(raw)          # validate
                out.write(raw + "\n")
                count += 1
            except json.JSONDecodeError:
                log.warning("archiver: skipping invalid JSON line in %s", src.name)
    return count


def _truncate(path: Path) -> None:
    """Overwrite *path* with empty content."""
    with path.open("w", encoding="utf-8") as fh:
        fh.write("")


def _cutoff_date() -> date:
    return date.today() - timedelta(days=365)


def _prune_jsonl(path: Path) -> None:
    """Remove rows older than 365 days. Keyed on 'date' or 'snapshot_date'."""
    if not path.exists():
        return
    cutoff = _cutoff_date()
    kept = []
    pruned = 0
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                kept.append(raw)
                continue
            row_date_str = row.get("date") or row.get("snapshot_date")
            if row_date_str:
                try:
                    row_date = date.fromisoformat(str(row_date_str)[:10])
                    if row_date < cutoff:
                        pruned += 1
                        continue
                except ValueError:
                    pass
            kept.append(raw)
    if pruned:
        log.info("archiver: pruned %d old rows from %s", pruned, path.name)
        with path.open("w", encoding="utf-8") as fh:
            for line in kept:
                fh.write(line + "\n")


def _prune_lessons_md(path: Path) -> None:
    """Remove dated section blocks (## YYYY-MM-DD) older than 365 days."""
    if not path.exists():
        return
    cutoff = _cutoff_date()
    content = path.read_text(encoding="utf-8")
    # Split into blocks on lines that start with "## YYYY-MM-DD"
    header_re = re.compile(r"^(## \d{4}-\d{2}-\d{2})", re.MULTILINE)
    parts = header_re.split(content)
    # parts: [preamble, header1, body1, header2, body2, ...]
    preamble = parts[0]
    blocks = []
    for i in range(1, len(parts), 2):
        header = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        date_str = header[3:].strip()
        try:
            block_date = date.fromisoformat(date_str)
        except ValueError:
            blocks.append((None, header, body))
            continue
        if block_date >= cutoff:
            blocks.append((block_date, header, body))
        else:
            log.info("archiver: pruning lessons block dated %s", date_str)

    rebuilt = preamble + "".join(h + b for _, h, b in blocks)
    if rebuilt != content:
        path.write_text(rebuilt, encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def archive_live_eod(data_dir: Path) -> None:
    """Archive end-of-day transient log files to master archives.

    Steps:
      1. Append transient JSONL files to their master counterparts.
      2. Append lessons.md to lessons_master.md, then clear it.
      3. Snapshot indicator_stats.json to indicator_stats_history.jsonl.
      4. Truncate each transient JSONL file.
      5. Prune master files to 365-day retention.
    """
    try:
        archive_dir = _ensure_archive_dir(data_dir)
    except Exception:
        log.exception("archiver: could not create archive directory -- aborting")
        return

    transient_to_master = [
        (data_dir / "decisions_log.jsonl",        archive_dir / "decisions_master.jsonl"),
        (data_dir / "outcomes.jsonl",              archive_dir / "outcomes_master.jsonl"),
        (data_dir / "indicator_outcomes.jsonl",    archive_dir / "indicator_outcomes_master.jsonl"),
    ]

    # ------------------------------------------------------------------
    # Step 1: Append transient JSONL -> master JSONL
    # ------------------------------------------------------------------
    for src, dst in transient_to_master:
        try:
            n = _append_jsonl(src, dst)
            if n:
                log.info("archiver: appended %d rows from %s to %s", n, src.name, dst.name)
        except Exception:
            log.exception("archiver: failed appending %s", src.name)

    # ------------------------------------------------------------------
    # Step 2: Append lessons.md -> lessons_master.md, then clear it
    # ------------------------------------------------------------------
    try:
        lessons_src = data_dir / "lessons.md"
        lessons_dst = archive_dir / "lessons_master.md"
        if lessons_src.exists():
            text = lessons_src.read_text(encoding="utf-8").strip()
            if text:
                with lessons_dst.open("a", encoding="utf-8") as fh:
                    fh.write(text + "\n---\n")
                log.info("archiver: appended lessons.md to lessons_master.md")
                _truncate(lessons_src)
    except Exception:
        log.exception("archiver: failed archiving lessons.md")

    # ------------------------------------------------------------------
    # Step 3: Snapshot indicator_stats.json
    # ------------------------------------------------------------------
    try:
        stats_src = data_dir / "indicator_stats.json"
        stats_dst = archive_dir / "indicator_stats_history.jsonl"
        if stats_src.exists():
            raw = stats_src.read_text(encoding="utf-8").strip()
            if raw:
                stats = json.loads(raw)
                stats["snapshot_date"] = date.today().isoformat()
                with stats_dst.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(stats) + "\n")
                log.info("archiver: snapshotted indicator_stats.json")
    except Exception:
        log.exception("archiver: failed snapshotting indicator_stats.json")

    # ------------------------------------------------------------------
    # Step 4: Truncate transient JSONL files
    # ------------------------------------------------------------------
    for src, _ in transient_to_master:
        try:
            if src.exists():
                _truncate(src)
        except Exception:
            log.exception("archiver: failed truncating %s", src.name)

    # ------------------------------------------------------------------
    # Step 5: Prune master files to 365-day retention
    # ------------------------------------------------------------------
    master_jsonl_files = [
        archive_dir / "decisions_master.jsonl",
        archive_dir / "outcomes_master.jsonl",
        archive_dir / "indicator_outcomes_master.jsonl",
        archive_dir / "indicator_stats_history.jsonl",
    ]
    for master in master_jsonl_files:
        try:
            _prune_jsonl(master)
        except Exception:
            log.exception("archiver: failed pruning %s", master.name)

    try:
        _prune_lessons_md(archive_dir / "lessons_master.md")
    except Exception:
        log.exception("archiver: failed pruning lessons_master.md")

    log.info("archiver: archive_live_eod complete")


# ---------------------------------------------------------------------------

def archive_backtest_run(results: dict, run_meta: dict, data_dir: Path) -> None:
    """Archive results from a completed backtest run.

    Steps:
      1. Append tagged decision rows to backtest_decisions_master.jsonl.
      2. Compute run summary and append to backtest_history.json.
      3. Truncate data/backtest_decisions.jsonl.
    """
    try:
        archive_dir = _ensure_archive_dir(data_dir)
    except Exception:
        log.exception("archiver: could not create archive directory -- aborting backtest archive")
        return

    run_id = run_meta.get("run_id", "")

    # ------------------------------------------------------------------
    # Step 1: Archive backtest decision rows
    # ------------------------------------------------------------------
    try:
        decisions = results.get("decisions_log", [])
        if decisions:
            dst = archive_dir / "backtest_decisions_master.jsonl"
            with dst.open("a", encoding="utf-8") as fh:
                for row in decisions:
                    if isinstance(row, dict):
                        row["run_id"] = run_id
                        fh.write(json.dumps(row) + "\n")
            log.info("archiver: archived %d backtest decision rows for run %s", len(decisions), run_id)
    except Exception:
        log.exception("archiver: failed archiving backtest decisions for run %s", run_id)

    # ------------------------------------------------------------------
    # Step 2: Compute run summary and append to backtest_history.json
    # ------------------------------------------------------------------
    try:
        trades = results.get("trades", [])
        curve = results.get("equity_curve", [])
        starting_cash = float(results.get("starting_cash", 100000))

        final_equity = float(curve[-1]["equity"]) if curve else starting_cash
        total_return_pct = round((final_equity / starting_cash - 1) * 100, 2) if starting_cash else 0

        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0]
        win_rate = round(len(wins) / len(trades), 4) if trades else 0
        gross_wins = sum(t["pnl"] for t in wins)
        gross_losses = abs(sum(t["pnl"] for t in losses))
        profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else None

        # Annualised Sharpe
        daily_rets = []
        prev = starting_cash
        for snap in curve:
            e = float(snap["equity"])
            if prev > 0:
                daily_rets.append((e - prev) / prev)
            prev = e
        if len(daily_rets) > 1:
            sd = statistics.stdev(daily_rets)
            sharpe = round(statistics.mean(daily_rets) / sd * (252 ** 0.5), 2) if sd > 0 else 0.0
        else:
            sharpe = 0.0

        # Max drawdown
        peak = starting_cash
        mdd = 0.0
        for snap in curve:
            e = float(snap["equity"])
            if e > peak:
                peak = e
            dd = (e - peak) / peak if peak > 0 else 0
            if dd < mdd:
                mdd = dd
        max_drawdown_pct = round(mdd * 100, 2)

        summary = {
            "run_id": run_id,
            "label": run_meta.get("label", ""),
            "date": datetime.now().date().isoformat(),
            "days": run_meta.get("days", 0),
            "results_file": str(run_meta.get("results_file", "")),
            "total_return_pct": total_return_pct,
            "win_rate": win_rate,
            "sharpe": sharpe,
            "max_drawdown_pct": max_drawdown_pct,
            "total_trades": len(trades),
            "profit_factor": profit_factor,
            "starting_cash": starting_cash,
            "final_equity": final_equity,
            "flags": run_meta.get("flags", []),
            "start_date": curve[0]["date"] if curve else None,
            "end_date": curve[-1]["date"] if curve else None,
        }

        history_path = data_dir / "backtest_history.json"
        if history_path.exists():
            try:
                history = json.loads(history_path.read_text(encoding="utf-8"))
                if not isinstance(history, list):
                    history = []
            except (json.JSONDecodeError, OSError):
                history = []
        else:
            history = []

        history.append(summary)
        history_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
        log.info("archiver: appended summary for run %s to backtest_history.json", run_id)

    except Exception:
        log.exception("archiver: failed computing/saving summary for run %s", run_id)

    # ------------------------------------------------------------------
    # Step 3: Truncate backtest_decisions.jsonl
    # ------------------------------------------------------------------
    try:
        bt_decisions = data_dir / "backtest_decisions.jsonl"
        if bt_decisions.exists():
            _truncate(bt_decisions)
            log.info("archiver: truncated backtest_decisions.jsonl")
    except Exception:
        log.exception("archiver: failed truncating backtest_decisions.jsonl")

    log.info("archiver: archive_backtest_run complete for run %s", run_id)
