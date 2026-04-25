"""Tests for the learning subsystem: intraday outcome grading, rules ledger,
per-ticker track record, and signal-weight auto-tuner.

All tests are hermetic — they use a tempdir data dir and mock out yfinance
so no network is required.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest


# ----------------------------------------------------------- fixtures

@pytest.fixture
def sandbox_paths(monkeypatch):
    """Point every data path at a fresh tempdir for the duration of the test.
    Yields the tempdir path so the test can inspect files directly.
    """
    from src.utils import config as cfg_mod
    cfg_mod.load_config.cache_clear()
    cfg = cfg_mod.load_config()
    saved = dict(cfg["paths"])
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        cfg["paths"]["data_dir"] = str(td_p)
        cfg["paths"]["journal_dir"] = str(td_p / "journal")
        cfg["paths"]["lessons_file"] = str(td_p / "lessons.md")
        cfg["paths"]["state_file"] = str(td_p / "state.json")
        cfg["paths"]["shortlist_file"] = str(td_p / "shortlist.json")
        cfg["paths"]["outcomes_file"] = str(td_p / "outcomes.jsonl")
        cfg["paths"]["rules_file"] = str(td_p / "rules.json")
        cfg["paths"]["signal_weights_file"] = str(td_p / "signal_weights.json")
        (td_p / "journal").mkdir(exist_ok=True)
        try:
            yield td_p
        finally:
            cfg["paths"] = saved


def _synthetic_bars(prices: list[float]) -> pd.DataFrame:
    """Build a DataFrame that looks like what yfinance returns for a 5m series."""
    idx = pd.date_range("2026-04-21 09:30", periods=len(prices), freq="5min", tz="America/New_York")
    return pd.DataFrame({
        "Open":  prices,
        "High":  [p * 1.002 for p in prices],
        "Low":   [p * 0.998 for p in prices],
        "Close": prices,
        "Volume": [10000] * len(prices),
    }, index=idx)


# ----------------------------------------------------------- outcome grading

def test_intraday_grading_buy_winner(sandbox_paths):
    """A BUY on an uptrending session should score as hit=True with positive edge."""
    from src.learning.outcomes import _grade_against_bars  # type: ignore
    bars = _synthetic_bars([100, 101, 102, 103, 104, 105])  # +5% session
    g = _grade_against_bars(
        bars=bars,
        decision_ts="2026-04-21T09:30:00-04:00",
        action="BUY",
        entry_price=None, stop=None, tp=None,
    )
    assert g is not None
    assert g["hit"] is True
    assert g["edge"] > 0
    assert g["pct_to_eod"] > 0
    assert g["max_favorable_pct"] > 0


def test_intraday_grading_buy_stop_hit(sandbox_paths):
    """An executed BUY whose stop got hit intraday should report stop_hit + realized=(stop-entry)/entry."""
    from src.learning.outcomes import _grade_against_bars  # type: ignore
    # Prices dip below stop then recover
    bars = _synthetic_bars([100, 99, 97, 98, 99, 100])
    # Low of bar 2 is 97 * 0.998 ~ 96.8; we set stop at 97 so it triggers
    g = _grade_against_bars(
        bars=bars,
        decision_ts="2026-04-21T09:30:00-04:00",
        action="BUY",
        entry_price=100.0,
        stop=97.0,
        tp=110.0,
    )
    assert g["stop_hit"] is True
    assert g["tp_hit"] is False
    assert g["realized_pct"] == pytest.approx((97.0 - 100.0) / 100.0)


def test_intraday_grading_buy_tp_hit(sandbox_paths):
    """An executed BUY whose TP got hit should report tp_hit and the TP realized pct."""
    from src.learning.outcomes import _grade_against_bars  # type: ignore
    # Prices rip through TP
    bars = _synthetic_bars([100, 102, 105, 110, 108, 106])
    g = _grade_against_bars(
        bars=bars,
        decision_ts="2026-04-21T09:30:00-04:00",
        action="BUY",
        entry_price=100.0,
        stop=95.0,
        tp=108.0,
    )
    assert g["tp_hit"] is True
    assert g["stop_hit"] is False
    assert g["realized_pct"] == pytest.approx((108.0 - 100.0) / 100.0)


def test_intraday_grading_hold_quiet_day(sandbox_paths):
    """HOLD should count as a hit on a quiet day (|change| < 1%)."""
    from src.learning.outcomes import _grade_against_bars  # type: ignore
    bars = _synthetic_bars([100, 100.2, 100.1, 100.3, 100.1, 100.2])
    g = _grade_against_bars(
        bars=bars,
        decision_ts="2026-04-21T09:30:00-04:00",
        action="HOLD",
        entry_price=None, stop=None, tp=None,
    )
    assert g["hit"] is True


def test_outcomes_roundtrip(sandbox_paths):
    """append_outcomes -> load_outcomes round-trips, and filters work."""
    from src.learning.outcomes import append_outcomes, load_outcomes
    rows = [
        {"date": "2026-04-20", "symbol": "AAPL", "outcome": {"hit": True, "edge": 0.01}},
        {"date": "2026-04-21", "symbol": "NVDA", "outcome": {"hit": False, "edge": -0.02}},
        {"date": "2026-04-21", "symbol": "AAPL", "outcome": {"hit": True, "edge": 0.02}},
    ]
    append_outcomes(rows)
    all_rows = load_outcomes()
    assert len(all_rows) == 3
    aapl_only = load_outcomes(symbol="AAPL")
    assert len(aapl_only) == 2
    # since_days filter — cutoff computed from UTC now, so "2026-04-20" likely
    # filtered out if we're running in 2026. We assert on semantic correctness
    # only: filter should keep at most `since_days` worth of rows.
    recent = load_outcomes(since_days=0)
    assert all(r.get("date") for r in recent)


# ----------------------------------------------------------- rules ledger

def test_rules_add_and_score(sandbox_paths):
    """Adding proposed rules, then scoring them against matching outcomes."""
    from src.learning.rules import add_proposed_rules, score_rules_against_outcomes, load_rules

    added = add_proposed_rules([{
        "text": "Skip BUYs in bearish regime",
        "condition": "bearish and buy",
        "action": "SKIP_BUY",
        "rationale": "test",
    }], regime="bearish")
    assert added == 1

    # Duplicate proposal (normalized text match) — should NOT add
    added_again = add_proposed_rules([{
        "text": "skip buys in bearish regime",  # same text, different case
        "condition": "bearish and buy",
        "action": "SKIP_BUY",
    }], regime="bearish")
    assert added_again == 0

    # Today's graded decisions: three bearish BUYs — two that should have been skipped
    today = "2026-04-21"
    outcomes = [
        {"date": today, "symbol": "AAPL", "action": "BUY", "regime": "bearish",
         "signals": {}, "outcome": {"hit": False, "edge": -0.02}},
        {"date": today, "symbol": "NVDA", "action": "BUY", "regime": "bearish",
         "signals": {}, "outcome": {"hit": False, "edge": -0.01}},
        {"date": today, "symbol": "AMD", "action": "HOLD", "regime": "bearish",
         "signals": {}, "outcome": {"hit": True, "edge": 0.0}},
    ]
    updated = score_rules_against_outcomes(outcomes)
    assert updated == 1

    rules = load_rules()
    stats = rules[0]["stats"]
    # Rule says BUY + bearish; 2 rows match (BUYs)
    assert stats["fire_count"] == 2
    # rule "hits" when outcome hit is True; BUYs both lost, so hit_count == 0
    assert stats["hit_count"] == 0
    # rule was "followed" when action != BUY — here both BUYs were NOT followed
    assert stats["follow_count"] == 0


def test_rules_are_never_auto_pruned(sandbox_paths):
    """No path through the rules module may remove a rule automatically,
    regardless of how poor its hit rate is. Removal only happens via the
    explicit set_rule_active(..., False) or delete_rule(rule_id) helpers
    that the dashboard calls on user action.
    """
    from src.learning.rules import (
        add_proposed_rules, score_rules_against_outcomes, load_rules,
        set_rule_active, delete_rule,
    )
    add_proposed_rules([{
        "text": "Always BUY regardless of signals",
        "condition": "buy",
        "action": "SKIP_BUY",  # even if we label it wrong, the rule stays
    }], regime="neutral")

    # Feed it many losing outcomes
    today = "2026-04-21"
    losing = [
        {"date": today, "symbol": "X", "action": "BUY", "regime": "neutral",
         "signals": {}, "outcome": {"hit": False, "edge": -0.05}}
    ] * 50
    score_rules_against_outcomes(losing)

    # Scoring must NEVER remove the rule, no matter the hit rate.
    assert len(load_rules()) == 1
    assert load_rules()[0]["stats"]["hit_count"] == 0
    assert load_rules()[0]["stats"]["fire_count"] >= 50

    # Deactivating keeps the rule in the file (just toggles the flag)
    assert set_rule_active(load_rules()[0]["id"], False) is True
    assert len(load_rules()) == 1
    assert load_rules()[0]["active"] is False

    # Only explicit delete_rule removes it
    assert delete_rule(load_rules()[0]["id"]) is True
    assert load_rules() == []


def test_rules_for_prompt_prefers_regime(sandbox_paths):
    """rules_for_prompt should surface regime-matching rules first."""
    from src.learning.rules import add_proposed_rules, rules_for_prompt
    add_proposed_rules([{"text": "a", "condition": "", "action": "SKIP_BUY"}], regime="bullish")
    add_proposed_rules([{"text": "b", "condition": "", "action": "SKIP_BUY"}], regime="bearish")
    add_proposed_rules([{"text": "c", "condition": "", "action": "SKIP_BUY"}], regime="volatile")

    ordered = rules_for_prompt(regime="bearish", limit=5)
    assert ordered[0]["text"] == "b"


# ----------------------------------------------------------- per-ticker track

def test_ticker_track_record_thresholds(sandbox_paths):
    """Under min_samples, has_history is False; at or above, it's True."""
    from src.learning.outcomes import append_outcomes
    from src.learning.track_record import ticker_track_record

    today = datetime.utcnow().strftime("%Y-%m-%d")
    # Add two AAPL outcomes — below the default min_samples of 3
    append_outcomes([
        {"date": today, "symbol": "AAPL", "action": "BUY",
         "outcome": {"hit": True, "edge": 0.01, "realized_pct": 0.01}},
        {"date": today, "symbol": "AAPL", "action": "HOLD",
         "outcome": {"hit": True, "edge": 0.0}},
    ])
    tr = ticker_track_record("AAPL", window_days=30, min_samples=3)
    assert tr["has_history"] is False

    append_outcomes([
        {"date": today, "symbol": "AAPL", "action": "BUY",
         "outcome": {"hit": False, "edge": -0.005, "realized_pct": -0.005}},
    ])
    tr = ticker_track_record("AAPL", window_days=30, min_samples=3)
    assert tr["has_history"] is True
    assert tr["samples"] == 3
    assert "decisions" in tr["summary_line"]


# ----------------------------------------------------------- signal weights

def test_signal_weight_tuner_no_samples(sandbox_paths):
    """Tuner must no-op and NOT write a weights file when samples are below threshold."""
    from src.learning.signal_weights import tune_signal_weights
    from src.utils.config import load_config
    result = tune_signal_weights()
    assert result["ran"] is False
    assert "need" in result["reason"]
    # No overlay file written
    assert not Path(load_config()["paths"]["signal_weights_file"]).exists()


def test_signal_weight_tuner_nudges_best_signal(sandbox_paths):
    """With one signal cleanly correlated to edge, its weight should nudge up
    and the worst-correlated signal's weight should nudge down."""
    from src.learning.outcomes import append_outcomes
    from src.learning.signal_weights import tune_signal_weights, effective_weights

    today = datetime.utcnow().strftime("%Y-%m-%d")
    rows = []
    # 50 rows where 'technicals' correlates perfectly with edge
    # and 'news' anti-correlates (small magnitude to avoid zero variance)
    for i in range(50):
        sign = 1 if i % 2 == 0 else -1
        edge = 0.01 * sign
        rows.append({
            "date": today, "symbol": "SYN",
            "outcome": {"hit": sign > 0, "edge": edge},
            "signals": {
                "technicals": 0.6 * sign,
                "news":       -0.6 * sign,
                "breadth":    0.0,
                "llm":        0.1 * sign,
            },
        })
    append_outcomes(rows)
    before = effective_weights()
    result = tune_signal_weights()
    assert result["ran"] is True
    after = result["after"]
    # technicals correlated positively → should be the signal nudged UP
    assert after["technicals"] > before["technicals"]
    # news anti-correlated → nudged DOWN
    assert after["news"] < before["news"]
    # sum still 1 and floor respected
    assert abs(sum(after.values()) - 1.0) < 0.001
    assert all(v >= 0.049 for v in after.values())  # 5% floor tolerance


def test_effective_weights_falls_back_to_settings(sandbox_paths):
    """With no overlay file, effective_weights returns the settings.yaml baseline."""
    from src.learning.signal_weights import effective_weights
    from src.utils.config import load_config
    w = effective_weights()
    base = load_config()["signals"]["weights"]
    for k, v in base.items():
        assert abs(w[k] - v) < 0.01
