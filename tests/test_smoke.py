"""Smoke tests — verify the project wires up end-to-end in sim mode.

These do NOT require any API keys or Webull access. They exercise the broker
factory, config loader, journaling, and reflection with mocked signals.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


def test_config_loads():
    from src.utils.config import load_config
    cfg = load_config()
    assert "broker" in cfg
    assert "trading" in cfg
    assert cfg["signals"]["weights"]
    # weights should sum to 1 (roughly)
    w = cfg["signals"]["weights"]
    assert abs(sum(w.values()) - 1.0) < 0.01


def test_sim_broker_basics():
    from src.broker import get_broker
    from src.broker.base import Order, OrderSide
    from src.utils.config import load_config

    cfg = load_config()
    cfg["broker"]["mode"] = "sim"

    broker = get_broker()
    acct = broker.get_account()
    assert acct.cash > 0
    assert acct.equity > 0


def test_journal_roundtrip():
    from src.utils import config as cfg_mod

    td = (Path("tests") / ".tmp" / "journal_roundtrip").resolve()
    td.mkdir(parents=True, exist_ok=True)
    for old in td.glob("*.jsonl"):
        old.unlink()

    cfg_mod.load_config.cache_clear()
    cfg = cfg_mod.load_config()
    original = cfg["paths"]["journal_dir"]
    cfg["paths"]["journal_dir"] = str(td)
    try:
        from src.learning.journal import append_entry, load_today_journal
        append_entry({"symbol": "AAPL", "decision": {"action": "BUY"}})
        entries = load_today_journal()
        assert len(entries) == 1
        assert entries[0]["symbol"] == "AAPL"
    finally:
        cfg["paths"]["journal_dir"] = original
        for old in td.glob("*.jsonl"):
            old.unlink()
        try:
            td.rmdir()
            td.parent.rmdir()
        except OSError:
            pass


def test_position_sizing():
    from src.broker.base import Account
    from src.trading.position_manager import compute_size

    acct = Account(cash=100_000, equity=100_000, buying_power=100_000, positions=[])
    # At $100/share and 5% per trade -> target ~$5000 -> 50 shares
    shares, details = compute_size(acct, 100.0)
    assert 45 <= shares <= 55
    assert details["sizing_mode"] == "normal"


def test_dynamic_stop_from_prior_candles():
    """Dynamic stop = min(low) of the last N candles *before* the current one.
    Width in % should vary trade to trade based on what the chart looks like.
    """
    from src.utils import config as cfg_mod
    from src.trading.position_manager import compute_dynamic_stop

    bars = pd.DataFrame({
        "open": [100, 101, 102, 103, 104],
        "high": [101, 102, 103, 104, 105],
        "low":  [ 99, 98.5, 97, 100, 103.5],   # last is the forming candle
        "close":[100.5, 101.5, 102.5, 103.5, 104.5],
        "volume":[1000]*5,
    })
    mock_broker = MagicMock()
    mock_broker.get_bars.return_value = bars

    cfg_mod.load_config.cache_clear()
    cfg = cfg_mod.load_config()
    original = cfg["trading"].get("stop_loss_max_pct")
    cfg["trading"]["stop_loss_max_pct"] = None
    try:
        # Entry at 104 -> prior candles (excluding last, so indices 0..3) have
        # lows [99, 98.5, 97, 100]. With lookback=2 we take the last 2: [97, 100].
        # min = 97. With no max-% clamp, stop should be exactly 97.
        result = compute_dynamic_stop(mock_broker, "TEST", 104.0)
        assert result["source"] == "dynamic"
        assert abs(result["stop"] - 97.0) < 0.01
    finally:
        cfg["trading"]["stop_loss_max_pct"] = original


def test_dynamic_stop_respects_optional_clamp(monkeypatch):
    """If the user sets stop_loss_max_pct, wide stops should be clamped."""
    from src.utils import config as cfg_mod
    from src.trading.position_manager import compute_dynamic_stop

    cfg_mod.load_config.cache_clear()
    cfg = cfg_mod.load_config()
    original = cfg["trading"].get("stop_loss_max_pct")
    cfg["trading"]["stop_loss_max_pct"] = 0.05
    try:
        bars = pd.DataFrame({
            "open": [100, 101, 102, 103, 104],
            "high": [101, 102, 103, 104, 105],
            "low":  [ 99, 98.5, 97, 100, 103.5],
            "close":[100.5, 101.5, 102.5, 103.5, 104.5],
            "volume":[1000]*5,
        })
        mock_broker = MagicMock()
        mock_broker.get_bars.return_value = bars
        result = compute_dynamic_stop(mock_broker, "TEST", 104.0)
        # Raw stop 97 is ~6.7% below 104 — with 5% cap, should clamp to 98.8
        assert result["source"] == "dynamic_clamped"
        assert abs(result["stop"] - 104.0 * 0.95) < 0.01
    finally:
        cfg["trading"]["stop_loss_max_pct"] = original


def test_dynamic_stop_respects_minimum_floor():
    """A tight candle-low stop should widen to the configured minimum stop pct."""
    from src.utils import config as cfg_mod
    from src.trading.position_manager import compute_dynamic_stop

    cfg_mod.load_config.cache_clear()
    cfg = cfg_mod.load_config()
    orig_min = cfg["trading"].get("stop_loss_min_pct")
    orig_pct = cfg["trading"].get("stop_loss_pct")
    orig_max = cfg["trading"].get("stop_loss_max_pct")
    cfg["trading"]["stop_loss_min_pct"] = 0.04
    cfg["trading"]["stop_loss_pct"] = 0.02
    cfg["trading"]["stop_loss_max_pct"] = 0.05
    try:
        bars = pd.DataFrame({
            "open": [100, 100, 100, 100, 100],
            "high": [101, 101, 101, 101, 101],
            "low":  [99.4, 99.2, 99.1, 99.0, 99.5],
            "close":[100.2, 100.3, 100.1, 100.4, 100.5],
            "volume":[1000]*5,
        })
        mock_broker = MagicMock()
        mock_broker.get_bars.return_value = bars
        result = compute_dynamic_stop(mock_broker, "TEST", 100.0)
        assert result["source"] == "dynamic_widened"
        assert abs(result["stop"] - 96.0) < 0.01
        assert abs(result["pct"] - 0.04) < 0.001
    finally:
        cfg["trading"]["stop_loss_min_pct"] = orig_min
        cfg["trading"]["stop_loss_pct"] = orig_pct
        cfg["trading"]["stop_loss_max_pct"] = orig_max


def test_fixed_stop_respects_minimum_floor():
    from src.broker.base import Position
    from src.utils import config as cfg_mod
    from src.trading.position_manager import compute_dynamic_stop, should_flatten_for_risk

    cfg_mod.load_config.cache_clear()
    cfg = cfg_mod.load_config()
    orig_mode = cfg["trading"].get("stop_loss_mode")
    orig_min = cfg["trading"].get("stop_loss_min_pct")
    orig_pct = cfg["trading"].get("stop_loss_pct")
    cfg["trading"]["stop_loss_mode"] = "fixed"
    cfg["trading"]["stop_loss_min_pct"] = 0.04
    cfg["trading"]["stop_loss_pct"] = 0.02
    try:
        result = compute_dynamic_stop(MagicMock(), "TEST", 100.0)
        assert result["source"] == "fixed_pct"
        assert abs(result["stop"] - 96.0) < 0.01
        assert should_flatten_for_risk(
            Position(symbol="TEST", quantity=1, avg_entry=100.0,
                     market_value=97.0, unrealized_pl=-3.0),
            97.0,
        ) == (False, "")
        should_flatten, why = should_flatten_for_risk(
            Position(symbol="TEST", quantity=1, avg_entry=100.0,
                     market_value=95.9, unrealized_pl=-4.1),
            95.9,
        )
        assert should_flatten is True
        assert "stop-loss hit" in why
    finally:
        cfg["trading"]["stop_loss_mode"] = orig_mode
        cfg["trading"]["stop_loss_min_pct"] = orig_min
        cfg["trading"]["stop_loss_pct"] = orig_pct


def test_trade_quality_classifier():
    from src.analysis.trade_quality import classify_trade_quality

    tech = {"score": 0.7}; news = {"score": 0.5}
    breadth = {"score": 0.3}; llm = {"score": 0.6}
    trend = {"label": "uptrend", "short": {"label": "uptrend"}}
    regime = {"label": "bullish"}
    q = classify_trade_quality(0.6, tech, news, breadth, llm, trend, regime)
    assert q["label"] == "strong"

    # Negative-news downgrade
    q2 = classify_trade_quality(
        0.6, tech, {"score": -0.5}, breadth, llm, trend, regime,
    )
    assert q2["label"] == "weak"


def test_position_sizing_risk_based_shrinks_on_wide_stop():
    """Wider stop = smaller size when risk-based sizing is on."""
    from src.broker.base import Account
    from src.trading.position_manager import compute_size

    acct = Account(cash=100_000, equity=100_000, buying_power=100_000, positions=[])
    # Tight stop: $100 entry, $98 stop -> $2 distance.
    # risk_budget = $100k * 1% = $1000 -> 500 shares by risk.
    # But % cap = $5000 / $100 = 50 shares -> % cap binds.
    qty_tight, d_tight = compute_size(acct, 100.0, stop_price=98.0)
    assert qty_tight == 50
    assert d_tight["binding_constraint"] == "pct_cap"

    # Wide stop: $100 entry, $90 stop -> $10 distance.
    # risk_budget = $1000 / $10 = 100 shares... but %-cap is 50.
    # Here % cap still binds. Let's go wider.
    # Very wide: $100 entry, $80 stop -> $20 distance.
    # risk_budget / $20 = 50 shares -> equals % cap, still % binds.
    # Extremely wide: $100 entry, $70 stop -> $30 distance.
    # risk_budget / $30 = 33 shares -> risk_budget binds, size drops to 33.
    qty_wide, d_wide = compute_size(acct, 100.0, stop_price=70.0)
    assert qty_wide < qty_tight
    assert qty_wide == 33
    assert d_wide["binding_constraint"] == "risk_budget"


def test_position_sizing_downtrend_halves():
    from src.broker.base import Account
    from src.trading.position_manager import compute_size

    acct = Account(cash=100_000, equity=100_000, buying_power=100_000, positions=[])
    trend = {"label": "downtrend",
             "short": {"label": "downtrend"},
             "long": {"label": "downtrend"}}
    shares, details = compute_size(acct, 100.0, trend=trend)
    # 2.5% of 100k at $100 = ~25 shares
    assert 20 <= shares <= 30
    assert details["sizing_mode"] == "downtrend_reduced"


def test_decision_with_mocked_signals():
    """Run decide_for_ticker with patched signal functions — no network."""
    from src.trading.decision_engine import decide_for_ticker
    from src.broker import get_broker
    from src.utils.config import load_config

    cfg = load_config()
    cfg["broker"]["mode"] = "sim"
    broker = get_broker()

    fake_tech = {"source": "technicals", "score": 0.8, "reason": "strong"}
    fake_news = {"source": "news", "score": 0.5, "reason": "bullish"}
    fake_breadth = {"source": "breadth", "score": 0.4, "reason": "ok", "details": {}}
    fake_llm = {"source": "llm", "score": 0.6, "action": "BUY",
                "confidence": 0.7, "reason": "looks good"}
    fake_trend = {"symbol": "TEST", "label": "uptrend",
                  "short": {"label": "uptrend", "change_pct": 0.05},
                  "long": {"label": "uptrend", "change_pct": 0.15},
                  "reason": "mocked"}
    fake_regime = {"label": "bullish", "score": 0.4, "reason": "mocked"}

    with patch("src.trading.decision_engine.technical_signal", return_value=fake_tech), \
         patch("src.trading.decision_engine.news_signal", return_value=fake_news), \
         patch("src.trading.decision_engine.llm_signal", return_value=fake_llm), \
         patch("src.trading.decision_engine.trend_classification", return_value=fake_trend), \
         patch("src.broker.sim.SimBroker.get_quote",
               return_value=type("Q", (), {"last": 150.0, "bid": 149.9, "ask": 150.1})()):
        result = decide_for_ticker(broker, "TEST", fake_breadth, fake_regime, position=None)
    assert result["action"] in ("BUY", "HOLD")
    assert result["combined_score"] > 0
    assert result["quality"]["label"] in ("strong", "normal", "weak")
    assert result["trend"]["label"] == "uptrend"
    assert result["regime"]["label"] == "bullish"


def test_llm_client_lmstudio_posts_openai_shape(monkeypatch):
    """With provider=lmstudio, chat() should POST an OpenAI-style payload to
    the configured base_url and return the assistant text."""
    from src.utils import config as cfg_mod
    from src.utils import llm_client

    cfg_mod.load_config.cache_clear()
    cfg = cfg_mod.load_config()
    cfg["llm"]["provider"] = "lmstudio"
    cfg["llm"]["base_url"] = "http://localhost:1234/v1"
    cfg["llm"]["model"] = "test-model"

    captured = {}

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "  hello from lmstudio  "}}]}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResp()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    out = llm_client.chat(prompt="ping", system="sys", max_tokens=50, temperature=0.1)
    assert out == "hello from lmstudio"  # stripped
    assert captured["url"] == "http://localhost:1234/v1/chat/completions"
    assert captured["json"]["model"] == "test-model"
    assert captured["json"]["messages"][0] == {"role": "system", "content": "sys"}
    assert captured["json"]["messages"][1] == {"role": "user", "content": "ping"}
    assert captured["json"]["max_tokens"] == 50
    # No Authorization header by default (LM Studio doesn't require one)
    assert "Authorization" not in (captured["headers"] or {})


def test_extract_json_object_handles_chatty_reasoning_models():
    """The shared extractor must cope with the four messes reasoning models
    produce: trailing prose, code fences, leading explanation, and the
    'Extra data' case that plain json.loads rejects."""
    from src.utils.llm_client import extract_json_object

    # 1) Trailing prose after the JSON (the "Extra data: line N column M" case)
    raw1 = '{"score": 0.5, "action": "BUY"}\n\nThis is because technicals look strong.'
    out1 = extract_json_object(raw1)
    assert out1 == {"score": 0.5, "action": "BUY"}

    # 2) Wrapped in a ```json ... ``` code fence
    raw2 = '```json\n{"score": -0.3, "summary": "mixed"}\n```'
    out2 = extract_json_object(raw2)
    assert out2 == {"score": -0.3, "summary": "mixed"}

    # 3) Leading prose before the JSON
    raw3 = 'Sure! Here is the classification:\n\n{"label": "bullish", "score": 0.4}'
    out3 = extract_json_object(raw3)
    assert out3["label"] == "bullish"

    # 4) Two JSON objects — take the first, ignore the second
    raw4 = '{"score": 0.2}\n{"note": "ignore me"}'
    out4 = extract_json_object(raw4)
    assert out4 == {"score": 0.2}

    # 5) Empty response → raises (caller falls back to non-LLM)
    import pytest as _p
    with _p.raises(ValueError):
        extract_json_object("")
    with _p.raises(ValueError):
        extract_json_object("no braces here at all")

    # 6) JSON with JS-style // comments mid-object (the AVGO failure mode)
    raw6 = (
        '{\n'
        '  "score": -0.28,\n'
        '  "action": "HOLD",\n'
        '  "confidence": 0.35,   // technicals overbought, news negative\n'
        '  "reason": "respect lessons"\n'
        '}'
    )
    out6 = extract_json_object(raw6)
    assert out6["action"] == "HOLD"
    assert out6["confidence"] == 0.35
    assert out6["reason"] == "respect lessons"

    # 7) // inside a string literal must NOT be treated as a comment
    raw7 = '{"url": "http://example.com/path", "score": 0.1}'
    out7 = extract_json_object(raw7)
    assert out7["url"] == "http://example.com/path"

    # 8) Trailing commas before } — common model output
    raw8 = '{"score": 0.5, "action": "BUY",}'
    out8 = extract_json_object(raw8)
    assert out8 == {"score": 0.5, "action": "BUY"}

    # 9) /* block comments */
    raw9 = '{"score": 0.2, /* thinking */ "action": "HOLD"}'
    out9 = extract_json_object(raw9)
    assert out9 == {"score": 0.2, "action": "HOLD"}


def test_llm_client_strips_reasoning_tags(monkeypatch):
    """Reasoning models (DeepSeek R1, QwQ) wrap their thinking in <think>...</think>
    before the real answer. chat() must strip those — the regime/news/advisor
    callers parse the response as JSON and would choke on the prefix otherwise."""
    from src.utils import config as cfg_mod
    from src.utils import llm_client

    cfg_mod.load_config.cache_clear()
    cfg = cfg_mod.load_config()
    cfg["llm"]["provider"] = "lmstudio"
    cfg["llm"]["base_url"] = "http://localhost:1234/v1"
    cfg["llm"]["model"] = "deepseek-r1"

    # Simulate a reasoning-model response: a long think block, then the answer
    raw = (
        "<think>\nThe user wants a market regime classification. Looking at the "
        "headlines, SPY is down 1.2%, VIX is up...\n</think>\n"
        '{"regime":"bearish","confidence":0.65}'
    )

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": raw}}]}

    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResp())

    out = llm_client.chat(prompt="classify", max_tokens=200)
    # Think block fully removed, JSON survives intact
    assert "<think>" not in out
    assert "</think>" not in out
    assert out.startswith("{")
    import json as _json
    parsed = _json.loads(out)
    assert parsed["regime"] == "bearish"

    # Also handle unterminated <think> blocks (model ran out of tokens
    # before closing the tag)
    raw2 = "<think>still thinking and never finished"

    class FakeResp2:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": raw2}}]}

    monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResp2())
    out2 = llm_client.chat(prompt="ping", max_tokens=20)
    assert out2 == ""  # nothing survives — caller will fall back, correctly


def test_llm_client_available_flags(monkeypatch):
    """llm_available() should reflect provider config: local always usable,
    anthropic requires a key."""
    from src.utils import config as cfg_mod
    from src.utils import llm_client

    cfg_mod.load_config.cache_clear()
    cfg = cfg_mod.load_config()

    cfg["llm"]["provider"] = "lmstudio"
    ok, reason = llm_client.llm_available()
    assert ok is True
    assert "lmstudio" in reason

    cfg["llm"]["provider"] = "anthropic"
    cfg["secrets"]["anthropic_api_key"] = ""
    ok, reason = llm_client.llm_available()
    assert ok is False
    assert "ANTHROPIC_API_KEY" in reason

    cfg["secrets"]["anthropic_api_key"] = "sk-test"
    ok, reason = llm_client.llm_available()
    assert ok is True


def test_position_sizing_regime_multiplier_shrinks_in_bearish():
    """Bearish regime should halve the % cap via regime_size_multiplier."""
    from src.broker.base import Account
    from src.trading.position_manager import compute_size

    acct = Account(cash=100_000, equity=100_000, buying_power=100_000, positions=[])
    # Baseline (bullish = 1.0x multiplier): 5% of 100k at $100 -> 50 shares
    qty_bull, det_bull = compute_size(acct, 100.0, regime={"label": "bullish"})
    # Bearish (0.5x multiplier): 2.5% of 100k at $100 -> ~25 shares
    qty_bear, det_bear = compute_size(acct, 100.0, regime={"label": "bearish"})
    assert qty_bull == 50
    assert qty_bear == 25
    assert det_bear["regime_mult"] == 0.5
    assert "regime_bearish" in det_bear["sizing_mode"]


def test_buy_queue_sorts_strong_before_weak():
    """Strong trades should jump ahead of weak ones even with lower scores,
    and within a quality tier the higher combined_score wins."""
    from src.trading.decision_engine import sort_buys_by_quality

    buys = [
        ("WEAK_HI", {"quality": {"label": "weak"},   "combined_score": 0.9}),
        ("NORM_LO", {"quality": {"label": "normal"}, "combined_score": 0.4}),
        ("STRONG_LO", {"quality": {"label": "strong"}, "combined_score": 0.4}),
        ("STRONG_HI", {"quality": {"label": "strong"}, "combined_score": 0.7}),
        ("NORM_HI", {"quality": {"label": "normal"}, "combined_score": 0.6}),
    ]
    ordered = [sym for sym, _ in sort_buys_by_quality(buys)]
    assert ordered == ["STRONG_HI", "STRONG_LO", "NORM_HI", "NORM_LO", "WEAK_HI"]


def test_bearish_regime_filters_weak_buys():
    """In a bearish regime with skip_weak_in_adverse_regimes enabled, a weak
    BUY should be downgraded to HOLD."""
    from src.trading.decision_engine import decide_for_ticker
    from src.broker import get_broker
    from src.utils.config import load_config

    cfg = load_config()
    cfg["broker"]["mode"] = "sim"
    broker = get_broker()

    # Signals tuned so combined score clears buy_threshold (0.35) -> action=BUY
    # pre-filter, then the adverse-regime quality gate should downgrade to HOLD
    # because we're forcing quality="weak" via the mock below.
    tech = {"source": "technicals", "score": 0.7, "reason": "mixed"}
    news = {"source": "news", "score": 0.4, "reason": "flat"}
    breadth = {"source": "breadth", "score": 0.2, "reason": "ok", "details": {}}
    llm = {"source": "llm", "score": 0.5, "action": "HOLD",
           "confidence": 0.4, "reason": "mild"}
    trend = {"symbol": "TEST", "label": "neutral",
             "short": {"label": "neutral"}, "long": {"label": "neutral"},
             "reason": "mocked"}
    bearish_regime = {"label": "bearish", "score": -0.5, "reason": "mocked"}

    with patch("src.trading.decision_engine.technical_signal", return_value=tech), \
         patch("src.trading.decision_engine.news_signal", return_value=news), \
         patch("src.trading.decision_engine.llm_signal", return_value=llm), \
         patch("src.trading.decision_engine.trend_classification", return_value=trend), \
         patch("src.trading.decision_engine.classify_trade_quality",
               return_value={"label": "weak", "score": 0.35, "reason": "forced weak"}):
        result = decide_for_ticker(broker, "TEST", breadth, bearish_regime, position=None)

    assert result["action"] == "HOLD"
    assert "weak quality in bearish regime" in result["reason"]


def test_backtest_entry_queue_fires_in_memory_bounce():
    from src.backtester.entry_queue import BacktestEntryQueue

    class FakeBroker:
        def get_bars(self, symbol, timeframe="15m", limit=10):
            return pd.DataFrame([
                {"open": 10.20, "high": 10.30, "low": 9.95, "close": 10.00, "volume": 1000},
                {"open": 10.05, "high": 10.30, "low": 10.02, "close": 10.20, "volume": 1200},
            ])

    queue = BacktestEntryQueue({"enabled": True, "bounce_touch_pct": 0.01})
    sim_dt = datetime(2026, 4, 27, 9, 30)
    queue.add_entry(
        symbol="TEST",
        entry_type="bounce_support",
        trigger_price=10.0,
        fib_ratio=0.618,
        fib_direction="support",
        combined_score=0.40,
        price_at_queue=10.50,
        deep_size_mult=0.75,
        sim_dt=sim_dt,
    )

    executed = []

    def execute_fn(symbol, tags, score, signals):
        executed.append((symbol, tags, score, signals))
        placed = MagicMock()
        placed.status = "filled"
        placed.quantity = 10
        placed.filled_price = 10.20
        return placed

    with patch("src.backtester.entry_queue.technical_signal",
               return_value={"score": 0.50, "details": {}, "reason": "mock"}), \
         patch("src.backtester.entry_queue.backtest_news_signal",
               return_value={"score": 0.40, "details": {}, "reason": "mock"}):
        fired = queue.check_and_fire(
            broker=FakeBroker(),
            sim_dt=datetime(2026, 4, 27, 9, 45),
            breadth={"score": 0.0, "reason": "mock"},
            regime={"label": "neutral", "score": 0.0},
            newsapi_key="",
            weights={"technicals": 0.35, "news": 0.15, "breadth": 0.20, "llm": 0.30},
            buy_threshold=0.35,
            use_llm=False,
            execute_fn=execute_fn,
            news_cache={},
        )

    assert fired
    assert queue.entries == []
    assert executed[0][0] == "TEST"
    assert executed[0][1]["deep_size_mult"] == 0.75
    assert any(row["event"] == "queued" for row in queue.history)
    assert any(row["event"] == "fired" for row in queue.history)


def test_trailing_stop_percentage_trail_only_raises():
    """Percentage trailing stop: stop = price * (1 - trail_pct), and only
    ever rises — pullbacks keep the old (higher) stop."""
    from src.trading.position_manager import compute_trailing_stop

    # Setup: entry was $10 with an 8% initial stop ($9.20). trail_pct = 0.08.
    # --- Price runs up to $12: candidate = 12 * 0.92 = 11.04 > 9.20 -> raise.
    r1 = compute_trailing_stop(current_stop=9.20, current_price=12.00, trail_pct=0.08)
    assert r1["raised"] is True
    assert abs(r1["new_stop"] - 11.04) < 0.001

    # --- Price pulls back to $11.50: candidate = 11.50 * 0.92 = 10.58.
    # Current stop (from prior trail) is 11.04 > 10.58 -> keep 11.04 (never lower).
    r2 = compute_trailing_stop(current_stop=11.04, current_price=11.50, trail_pct=0.08)
    assert r2["raised"] is False
    assert r2["new_stop"] == 11.04

    # --- A wider-volatility stock with a 12% trail behaves the same way.
    # Entry $10, stop $8.80 (12% trail). Price runs to $15: candidate = 13.20.
    r3 = compute_trailing_stop(current_stop=8.80, current_price=15.00, trail_pct=0.12)
    assert r3["raised"] is True
    assert abs(r3["new_stop"] - 13.20) < 0.001
