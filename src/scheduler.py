"""APScheduler wiring for the daily trading schedule.

Schedule (all times EST):
  04:30  pre_market_430   — lessons log + full watchlist build + deep score all
                            stale tickers + weak-ticker cull
  06:30  pre_market_refresh — holdings status + watchlist refresh + compare +
                            stale-only deep score + weak-ticker cull
  07:30  pre_market_refresh (same)
  08:30  pre_market_refresh (same)
  09:00  trade_plan       — final refresh + stale-only score + LLM synthesis
                            -> narrows watchlist to top 20
  09:30  decision cycle   — place trades with stops
  11:30  decision cycle
  12:30  decision cycle
  13:30  decision cycle
  14:30  decision cycle
  15:30  decision cycle   — flatten_on_weak_close check fires here
  16:30  eod_audit        — EOD reflection + learning
  Fri 17:00  signal_weight_tuner
"""
from __future__ import annotations

import json
import time
from functools import wraps
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .analysis import classify_market_regime
from .broker import get_broker
from .learning import run_eod_reflection
from .learning.signal_weights import tune_signal_weights
from .screener import build_shortlist
from .screener.pre_market import (
    filter_and_replace_weak_tickers,
    get_premarket_ratings,
    load_shortlist,
)
from .trading import run_decision_cycle
from .trading import entry_queue
from .utils.config import load_config
from .utils.logger import get_logger
from .utils.market_time import is_trading_day

log = get_logger(__name__)


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins, secs = divmod(int(seconds), 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hrs, mins = divmod(mins, 60)
    return f"{hrs}h {mins}m {secs}s"


def _time_job(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - started
            log.info(
                f"[timing] {func.__name__} completed in {_format_duration(elapsed)} "
                f"({elapsed:.1f}s)"
            )
    return wrapper


def _skip_if_not_trading_day(func):
    def wrapper(*args, **kwargs):
        if load_config()["scheduler"]["skip_holidays"] and not is_trading_day():
            log.info(f"[{func.__name__}] skipping — not a trading day")
            return
        return func(*args, **kwargs)
    return wrapper


# ------------------------------------------------------------------ helpers

def _llm_ready() -> bool:
    """Ping the LLM server with a 15-second timeout. Returns True if reachable."""
    from .utils.llm_client import llm_ping
    result = llm_ping(timeout=15.0)
    if not result.get("ok"):
        log.warning(
            f"[llm-check] LLM not reachable ({result.get('error', 'unknown')}) — "
            "jobs will fall back to no-LLM mode"
        )
    return bool(result.get("ok"))


def _log_lessons() -> None:
    """Print a brief summary of current rules/lessons to the log."""
    cfg = load_config()
    rules_path = Path(cfg["paths"]["rules_file"])
    lessons_path = Path(cfg["paths"]["lessons_file"])
    rule_count = 0
    if rules_path.exists():
        try:
            rules = json.loads(rules_path.read_text(encoding="utf-8"))
            rule_count = len(rules) if isinstance(rules, list) else len(rules.get("rules", []))
        except Exception:
            pass
    lesson_lines = 0
    if lessons_path.exists():
        try:
            lesson_lines = len(lessons_path.read_text(encoding="utf-8").splitlines())
        except Exception:
            pass
    log.info(f"[lessons] {rule_count} active rules | lessons file: {lesson_lines} lines")


def _run_stale_deep_score(symbols: list[str], full_run: bool = False) -> None:
    """Score tickers that have no fresh deep score.

    full_run=True: score ALL stale tickers (used at 04:30).
    full_run=False: score only tickers added since last full run (fast pass).
    """
    try:
        from .analysis.deep_scorer import get_score, score_tickers
        stale = [s for s in symbols if get_score(s) is None]
        if not stale:
            log.info("[deep-score] all tickers have fresh scores")
            return
        label = "full" if full_run else "stale-only"
        log.info(f"[deep-score] {label} pass: scoring {len(stale)} tickers — {stale}")
        score_tickers(stale)
    except Exception as e:
        log.exception(f"[deep-score] scoring pass failed: {e}")


def _log_holdings(broker) -> None:
    """Log P&L and stop distance for each open position."""
    try:
        account = broker.get_account()
        if not account.positions:
            log.info("[holdings] no open positions")
            return
        for p in account.positions:
            cost_basis = p.market_value - p.unrealized_pl
            pnl_pct = (p.unrealized_pl / cost_basis * 100) if cost_basis else 0
            stop = getattr(p, "stop_loss", None) or (p.tags or {}).get("stop_loss")
            stop_str = f"stop={stop:.2f}" if stop else "no stop"
            log.info(
                f"[holdings] {p.symbol}: qty={p.quantity} | "
                f"P&L={pnl_pct:+.1f}% | {stop_str}"
            )
    except Exception as e:
        log.warning(f"[holdings] review failed: {e}")


def _compare_shortlists(previous: list[str], current: list[str]) -> None:
    """Log additions and removals between two shortlist snapshots."""
    prev_set = set(previous)
    curr_set = set(current)
    added = sorted(curr_set - prev_set)
    removed = sorted(prev_set - curr_set)
    if added:
        log.info(f"[shortlist-diff] added: {added}")
    if removed:
        log.info(f"[shortlist-diff] removed: {removed}")
    if not added and not removed:
        log.info("[shortlist-diff] no changes since last build")


def _handle_empty_watchlist(broker) -> None:
    """Close all positions and log when the watchlist has been emptied by the cull."""
    log.warning("[watchlist] empty watchlist — closing all positions, bot goes to cash")
    try:
        orders = broker.flatten_all()
        log.info(f"[watchlist] flattened {len(orders)} positions")
    except Exception as e:
        log.exception(f"[watchlist] flatten failed: {e}")


# ------------------------------------------------------------------ jobs

@_skip_if_not_trading_day
@_time_job
def job_pre_market_430() -> None:
    """04:30 — full pre-market scan: lessons + watchlist + deep score all stale + cull."""
    log.info("=== pre-market 04:30 — full scan ===")
    broker = get_broker()

    _log_lessons()
    _llm_ready()  # warm check — logs warning but doesn't abort (jobs have LLM fallbacks)

    try:
        regime = classify_market_regime(force=True)
        log.info(f"[regime] {regime['label'].upper()} — {regime['reason'][:160]}")
    except Exception as e:
        log.warning(f"[regime] classification failed: {e}")

    shortlist = build_shortlist()

    # Pre-market tech + news ratings for every ticker
    ratings = get_premarket_ratings(shortlist, broker=broker)

    # Full deep score pass — score ALL stale tickers (the day's main scoring run)
    _run_stale_deep_score(shortlist, full_run=True)

    # Cull weak tickers and backfill; handle empty result
    filtered = filter_and_replace_weak_tickers(shortlist, ratings, broker=broker)
    if not filtered:
        _handle_empty_watchlist(broker)


@_skip_if_not_trading_day
@_time_job
def job_pre_market_refresh(label: str) -> None:
    """06:30 / 07:30 / 08:30 — incremental refresh: holdings + new watchlist + stale score + cull."""
    log.info(f"=== pre-market refresh [{label}] ===")
    broker = get_broker()

    # Holdings status before the day begins
    _log_holdings(broker)

    try:
        regime = classify_market_regime(force=True)
        log.info(f"[regime] {regime['label'].upper()} — {regime['reason'][:80]}")
    except Exception as e:
        log.warning(f"[regime] classification failed: {e}")

    previous = load_shortlist()
    shortlist = build_shortlist()
    _compare_shortlists(previous, shortlist)

    # Rate only newly added tickers (others rated at 04:30)
    new_syms = [s for s in shortlist if s not in set(previous)]
    ratings: dict = {}
    if new_syms:
        ratings = get_premarket_ratings(new_syms, broker=broker)

    # Stale-only deep score: just tickers that still have no fresh score
    _run_stale_deep_score(shortlist, full_run=False)

    # Cull + backfill
    filtered = filter_and_replace_weak_tickers(shortlist, ratings, broker=broker)
    if not filtered:
        _handle_empty_watchlist(broker)


@_skip_if_not_trading_day
@_time_job
def job_trade_plan() -> None:
    """09:00 — final watchlist refresh + LLM synthesis -> top-20 trade plan."""
    log.info("=== trade plan [09:00] ===")
    _llm_ready()
    broker = get_broker()

    previous = load_shortlist()
    shortlist = build_shortlist()
    _compare_shortlists(previous, shortlist)

    new_syms = [s for s in shortlist if s not in set(previous)]
    ratings: dict = {}
    if new_syms:
        ratings = get_premarket_ratings(new_syms, broker=broker)

    # Score any remaining unscored tickers (should be very few at this point)
    _run_stale_deep_score(shortlist, full_run=False)

    # Cull weak tickers one final time
    filtered = filter_and_replace_weak_tickers(shortlist, ratings, broker=broker)
    if not filtered:
        _handle_empty_watchlist(broker)
        return

    # LLM synthesis — rank the filtered list and narrow to top 20
    try:
        from .analysis.trade_planner import build_trade_plan
        regime_label = "neutral"
        try:
            regime_label = classify_market_regime().get("label", "neutral")
        except Exception:
            pass
        all_ratings = get_premarket_ratings(filtered, broker=broker)
        build_trade_plan(filtered, all_ratings, regime_label=regime_label)
    except Exception as e:
        log.exception(f"[trade-plan] LLM trade plan failed: {e}")


@_skip_if_not_trading_day
def job_pre_cycle_context(label: str) -> None:
    """5 min before each intraday decision cycle — snapshot open positions at
    live prices and write today_context.md so the upcoming cycle's LLM advisor
    sees genuinely current session data, not data from when the last cycle ended.
    """
    log.info(f"=== pre-cycle session snapshot [before {label}] ===")
    try:
        broker = get_broker()
        from .learning.session_context import update_session_context
        update_session_context(broker, f"pre-{label}")
    except Exception as e:
        log.exception(f"pre-cycle context update failed: {e}")


@_skip_if_not_trading_day
@_time_job
def job_decision(label: str) -> None:
    log.info(f"=== decision cycle {label} ===")
    # Guard: skip if watchlist is empty (bot went to cash this morning)
    current = load_shortlist()
    if not current:
        log.info(f"[decision] skipping {label} — watchlist is empty (no-trade day)")
        return
    broker = get_broker()
    run_decision_cycle(broker, label)


@_skip_if_not_trading_day
def job_entry_monitor() -> None:
    """Every-5-minutes intraday monitor — checks queued entries for S/R triggers."""
    pending = entry_queue.get_entries()
    if not pending:
        return
    log.info(f"[entry_monitor] checking {len(pending)} queued entries")
    try:
        broker = get_broker()

        def _execute(b, symbol: str, tags: dict) -> None:
            from .trading.decision_engine import _place_queued_buy  # noqa: PLC0415
            _place_queued_buy(b, symbol, tags)

        fired = entry_queue.check_and_fire(broker, _execute)
        if fired:
            log.info(f"[entry_monitor] fired: {fired}")
    except Exception as e:
        log.exception(f"[entry_monitor] error: {e}")


def _git_push_eod(date_str: str) -> None:
    """Stage all tracked files, commit, and push to GitHub after EOD."""
    import subprocess
    from .utils.config import project_root
    root = str(project_root())
    try:
        subprocess.run(["git", "add", "."], cwd=root, capture_output=True, timeout=30)
        result = subprocess.run(
            ["git", "commit", "-m", f"Auto: EOD backup {date_str}"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        committed = result.returncode == 0
        push = subprocess.run(["git", "push"], cwd=root, capture_output=True, text=True, timeout=60)
        if push.returncode == 0:
            log.info(f"EOD git push complete (new commit: {committed})")
        else:
            log.warning(f"EOD git push failed: {push.stderr.strip()[:200]}")
    except Exception as _e:
        log.warning(f"EOD git push skipped: {_e}")


@_skip_if_not_trading_day
@_time_job
def job_eod() -> None:
    log.info("=== end-of-day reflection ===")
    broker = get_broker()
    entry_queue.log_eod_outcomes()  # record close prices before clearing cache
    entry_queue.expire_entries()    # clear any remaining queued entries
    run_eod_reflection(broker)
    from datetime import date as _date
    _git_push_eod(_date.today().isoformat())


@_time_job
def job_tune_signal_weights() -> None:
    log.info("=== signal-weight tuner ===")
    try:
        result = tune_signal_weights()
        if result.get("ran"):
            after = result.get("after", {})
            log.info(
                "weights updated: "
                + ", ".join(f"{k}={v:.3f}" for k, v in after.items())
            )
        else:
            log.info(f"weight tuner skipped: {result.get('reason')}")
    except Exception as e:
        log.exception(f"signal-weight tuner failed: {e}")

    # Weekly rule retirement check — flags weak rules for dashboard review
    try:
        from .learning.rules import flag_weak_rules_for_retirement
        flagged = flag_weak_rules_for_retirement()
        if flagged:
            log.info(f"[rules] {len(flagged)} rule(s) flagged for retirement review: {flagged}")
        else:
            log.info("[rules] no rules flagged for retirement this week")
    except Exception as e:
        log.exception(f"rule retirement check failed: {e}")


# ------------------------------------------------------------------ catch-up on restart

_DECISION_SCHEDULE = [
    (9,  30, "09:30"),
    (11, 30, "11:30"),
    (12, 30, "12:30"),
    (13, 30, "13:30"),
    (14, 30, "14:30"),
    (15, 30, "15:30"),
]


def _fire_catchup_jobs(sched, tz_str: str) -> None:
    """On restart, fire the most recently missed decision cycle and EOD (if past 16:30).

    Only the single most recent missed decision cycle is fired — running all
    missed cycles back-to-back is redundant since each evaluates current prices.
    Duplicate buys are impossible: job_decision -> run_decision_cycle checks held
    positions before every BUY and skips symbols already owned.
    Pre-market setup jobs are not re-run; their output (shortlist.json) persists.
    """
    from datetime import datetime, timedelta
    import pytz

    tz = pytz.timezone(tz_str)
    now = datetime.now(tz)

    # Always validate the queue cache on restart — drop entries with no signal,
    # keep valid ones so the 5-min monitor can still fire them without re-running
    # the full decision cycle.
    sched.add_job(
        lambda: entry_queue.validate_on_restart(get_broker()),
        "date",
        run_date=now + timedelta(seconds=3),
        name="startup_queue_validation",
    )

    if not is_trading_day():
        return

    today = now.date()

    def _dt(h: int, m: int) -> datetime:
        return tz.localize(datetime(today.year, today.month, today.day, h, m))

    # Find the most recently missed decision cycle
    missed_label: str | None = None
    for h, m, lbl in _DECISION_SCHEDULE:
        if _dt(h, m) < now:
            missed_label = lbl

    delay = 5  # seconds after sched.start() before the catch-up fires

    if missed_label:
        run_at = now + timedelta(seconds=delay)
        log.info(
            f"[catchup] restart at {now.strftime('%H:%M')} ET — "
            f"firing missed decision cycle {missed_label} in {delay}s"
        )
        sched.add_job(
            job_decision, "date",
            run_date=run_at,
            args=[missed_label],
            name=f"catchup_decide_{missed_label}",
        )
        delay += 15

    if _dt(16, 30) < now:
        run_at = now + timedelta(seconds=delay)
        log.info(f"[catchup] restart after 16:30 ET — firing missed EOD audit in {delay}s")
        sched.add_job(
            job_eod, "date",
            run_date=run_at,
            name="catchup_eod_audit",
        )


# ------------------------------------------------------------------ scheduler

def run_forever() -> None:
    cfg = load_config()["scheduler"]
    sched = BlockingScheduler(timezone=cfg["timezone"])
    dow = "mon-fri"

    # 04:30 — full pre-market scan
    sched.add_job(job_pre_market_430, CronTrigger(hour=4, minute=30, day_of_week=dow),
                  name="pre_market_430")

    # 06:30 / 07:30 / 08:30 — incremental refreshes
    for h, m, lbl in [(6, 30, "06:30"), (7, 30, "07:30"), (8, 30, "08:30")]:
        sched.add_job(
            job_pre_market_refresh,
            CronTrigger(hour=h, minute=m, day_of_week=dow),
            args=[lbl], name=f"pre_market_refresh_{lbl}",
        )

    # 09:00 — LLM trade plan, narrows to top 20
    sched.add_job(job_trade_plan, CronTrigger(hour=9, minute=0, day_of_week=dow),
                  name="trade_plan_0900")

    # Session context snapshots: 5 min before each intraday cycle (not the open)
    # so the cycle's LLM advisor sees live prices, not stale end-of-prior-cycle data.
    for h, m, lbl in [
        (11, 25, "11:30"),
        (12, 25, "12:30"),
        (13, 25, "13:30"),
        (14, 25, "14:30"),
        (15, 25, "15:30"),
    ]:
        sched.add_job(
            job_pre_cycle_context,
            CronTrigger(hour=h, minute=m, day_of_week=dow),
            args=[lbl], name=f"pre_cycle_ctx_{lbl}",
        )

    # Decision cycles: open + 5 intraday checks
    for h, m, lbl in [
        (9,  30, "09:30"),
        (11, 30, "11:30"),
        (12, 30, "12:30"),
        (13, 30, "13:30"),
        (14, 30, "14:30"),
        (15, 30, "15:30"),
    ]:
        sched.add_job(
            job_decision,
            CronTrigger(hour=h, minute=m, day_of_week=dow),
            args=[lbl], name=f"decide_{lbl}",
        )

    # Intraday entry-queue monitor: every 5 minutes during market hours
    sched.add_job(
        job_entry_monitor,
        CronTrigger(hour="9-15", minute="*/5", day_of_week=dow),
        name="entry_queue_monitor",
    )

    # EOD reflection
    sched.add_job(job_eod, CronTrigger(hour=16, minute=30, day_of_week=dow),
                  name="eod_audit")

    # Weekly signal-weight tuner (Friday after EOD)
    sched.add_job(job_tune_signal_weights,
                  CronTrigger(day_of_week="fri", hour=17, minute=0),
                  name="signal_weight_tuner")

    # Schedule catch-up jobs for any cycles missed before this restart
    _fire_catchup_jobs(sched, cfg["timezone"])

    log.info("Scheduler starting. Jobs:")
    for j in sched.get_jobs():
        nxt = getattr(j, "next_run_time", None)
        if nxt is None:
            try:
                from datetime import datetime
                import pytz
                tz = pytz.timezone(cfg["timezone"])
                nxt = j.trigger.get_next_fire_time(None, datetime.now(tz))
            except Exception:
                nxt = "(will compute on start)"
        log.info(f"  - {j.name}: next run {nxt}")

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped by user")
        raise
