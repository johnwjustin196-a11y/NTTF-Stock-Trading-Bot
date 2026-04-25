"""Analysis modules — each produces a normalized signal dict for a ticker."""
from .technicals import technical_signal
from .news_sentiment import news_signal
from .market_breadth import breadth_signal
from .llm_advisor import llm_signal
from .trend import trend_classification, is_downtrend
from .market_regime import classify_market_regime
from .trade_quality import classify_trade_quality
from .deep_scorer import score_ticker, score_tickers, get_score, deep_score_gate, load_scores

__all__ = [
    "technical_signal",
    "news_signal",
    "breadth_signal",
    "llm_signal",
    "trend_classification",
    "is_downtrend",
    "classify_market_regime",
    "classify_trade_quality",
    "score_ticker",
    "score_tickers",
    "get_score",
    "deep_score_gate",
    "load_scores",
]
