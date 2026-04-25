"""Trade quality classification.

Labels every *BUY* decision as one of:
    strong   - high-confidence setup: signals agree, trend aligned, news friendly
    normal   - decent but not standout
    weak     - thin edge — only taken if other gates still permit, and sized down

The label is derived purely from the signals we already have; no new network
calls. The intent is to give the dashboard and the journal a human-legible tag
for each trade, and to optionally feed position sizing later.
"""
from __future__ import annotations

from typing import Any

from ..utils.config import load_config


def classify_trade_quality(
    combined_score: float,
    tech: dict,
    news: dict,
    breadth: dict,
    llm: dict,
    trend: dict | None,
    regime: dict | None,
) -> dict[str, Any]:
    """Return {'label': 'strong'|'normal'|'weak', 'reason': str, 'agreement': int}."""
    cfg = load_config().get("trade_quality", {}) or {}
    strong_min = float(cfg.get("strong_min_score", 0.55))
    weak_max = float(cfg.get("weak_max_score", 0.40))
    neg_news_thresh = float(cfg.get("downgrade_on_negative_news", -0.3))

    tech_s = float((tech or {}).get("score", 0.0))
    news_s = float((news or {}).get("score", 0.0))
    breadth_s = float((breadth or {}).get("score", 0.0))
    llm_s = float((llm or {}).get("score", 0.0))

    # Agreement: how many of the 4 signals are bullish (> 0.1)
    agreement = sum(1 for s in (tech_s, news_s, breadth_s, llm_s) if s > 0.1)

    trend_label = (trend or {}).get("label", "unknown")
    regime_label = (regime or {}).get("label", "neutral")

    # Base label from combined score
    if combined_score >= strong_min:
        label = "strong"
    elif combined_score <= weak_max:
        label = "weak"
    else:
        label = "normal"

    reasons: list[str] = [
        f"combined={combined_score:+.2f}",
        f"agreement={agreement}/4",
        f"trend={trend_label}",
        f"regime={regime_label}",
    ]

    # Modifiers
    if news_s <= neg_news_thresh:
        label = "weak"
        reasons.append(f"downgrade: negative news ({news_s:+.2f})")

    if trend_label in ("downtrend", "bounce_in_downtrend") and label == "strong":
        label = "normal"
        reasons.append("downgrade: trading against 30d trend")

    if regime_label == "bearish" and label == "strong":
        label = "normal"
        reasons.append("downgrade: bearish market regime")

    if regime_label == "volatile" and label != "weak":
        # In a chop tape, 'strong' becomes 'normal' and 'normal' stays
        if label == "strong":
            label = "normal"
            reasons.append("downgrade: volatile regime")

    # Promote: if every signal agrees and regime is bullish, ensure "strong"
    if agreement == 4 and regime_label == "bullish" and label == "normal":
        label = "strong"
        reasons.append("promote: full signal agreement in bullish regime")

    return {
        "label": label,
        "agreement": agreement,
        "reason": "; ".join(reasons),
    }
