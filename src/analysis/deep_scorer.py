"""Comprehensive 5-dimension stock scorer.

Mirrors the /trade analyze pipeline but runs entirely inside the bot using
yfinance data + the configured local LLM (LM Studio / any OpenAI-compat server).
No web-search agents required.

Five scoring dimensions (weights match /trade analyze):
  Technical   25%  — price action, indicators, trend, relative strength
  Fundamental 25%  — revenue, margins, valuation, balance sheet
  Sentiment   20%  — news, analyst ratings, short interest, institutional
  Risk        15%  — volatility, drawdown, liquidity, debt, dilution risk
  Thesis      15%  — LLM synthesis of all four into conviction score

Composite Trade Score (0–100) and letter grade are written to
``data/trade_scores.json`` so the decision engine can use them as a
gate / position-size multiplier without slowing the intraday cycle.

Usage (CLI):
  python -m src.analysis.deep_scorer                    # score shortlist
  python -m src.analysis.deep_scorer BBAI NVDA TSLA     # specific tickers

Usage (in code):
  from src.analysis.deep_scorer import score_ticker, get_score, deep_score_gate
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import logging

import numpy as np
import pandas as pd
import yfinance as yf

# yfinance logs HTTP 404s at ERROR level for ETFs/indices that have no
# fundamentals data. We handle those gracefully in try/except, so silence
# yfinance's own logger to keep the output clean.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

from ..utils.config import load_config
from ..utils.llm_client import chat, extract_json_object, llm_available
from ..utils.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Grade / signal table — identical to /trade analyze skill
# ---------------------------------------------------------------------------
_GRADES = [
    (85, "A+", "strong_buy"),
    (70, "A",  "buy"),
    (55, "B",  "accumulate"),
    (40, "C",  "neutral"),
    (25, "D",  "caution"),
    (0,  "F",  "avoid"),
]

_WEIGHTS = {"technical": 0.25, "fundamental": 0.25,
            "sentiment": 0.20, "risk": 0.15, "thesis": 0.15}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _safe(val: Any, default: Any = None) -> Any:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return default
    return val


def _pct(val: Any, default: str = "n/a") -> str:
    v = _safe(val)
    if v is None:
        return default
    return f"{v * 100:.1f}%"


def _usd(val: Any, default: str = "n/a") -> str:
    v = _safe(val)
    if v is None:
        return default
    if abs(v) >= 1e9:
        return f"${v / 1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:.1f}M"
    return f"${v:,.0f}"


def _num(val: Any, decimals: int = 2, default: str = "n/a") -> str:
    v = _safe(val)
    if v is None:
        return default
    return f"{v:.{decimals}f}"


def _grade(score: float) -> tuple[str, str]:
    for threshold, letter, signal in _GRADES:
        if score >= threshold:
            return letter, signal
    return "F", "avoid"


# ---------------------------------------------------------------------------
# Technical indicator computation (daily bars, no broker needed)
# ---------------------------------------------------------------------------

def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    dn = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / dn.replace(0, np.nan)
    series = 100 - 100 / (1 + rs)
    val = series.iloc[-1]
    return float(val) if pd.notna(val) else 50.0


def _macd(close: pd.Series) -> tuple[float, float]:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    line = ema12 - ema26
    signal = line.ewm(span=9, adjust=False).mean()
    hist = (line - signal).iloc[-1]
    sig_val = signal.iloc[-1]
    return (float(hist) if pd.notna(hist) else 0.0,
            float(sig_val) if pd.notna(sig_val) else 0.0)


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if pd.notna(val) else 0.0


def _bollinger(close: pd.Series, period: int = 20, n_std: float = 2.0) -> dict:
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = (mid + n_std * std).iloc[-1]
    lower = (mid - n_std * std).iloc[-1]
    mid_val = mid.iloc[-1]
    price = close.iloc[-1]
    if pd.notna(upper) and pd.notna(lower) and (upper - lower) > 0:
        position = (price - lower) / (upper - lower)
    else:
        position = 0.5
    return {"upper": float(upper) if pd.notna(upper) else None,
            "lower": float(lower) if pd.notna(lower) else None,
            "mid": float(mid_val) if pd.notna(mid_val) else None,
            "position": float(position)}


def _sma(close: pd.Series, period: int) -> float | None:
    if len(close) < period:
        return None
    val = close.rolling(period).mean().iloc[-1]
    return float(val) if pd.notna(val) else None


def _perf(close: pd.Series, n: int) -> float | None:
    if len(close) < n + 1:
        return None
    start = close.iloc[-(n + 1)]
    end = close.iloc[-1]
    if start <= 0:
        return None
    return float((end - start) / start)


def _compute_indicators(hist: pd.DataFrame) -> dict:
    """Return a flat dict of all computed technical indicators."""
    close = hist["Close"].astype(float)
    price = float(close.iloc[-1])

    sma20 = _sma(close, 20)
    sma50 = _sma(close, 50)
    sma200 = _sma(close, 200)

    def vs_ma(ma):
        if ma and ma > 0:
            return (price - ma) / ma
        return None

    rsi = _rsi(close)
    macd_hist, macd_sig = _macd(close)
    atr = _atr(hist)
    bb = _bollinger(close)

    returns = close.pct_change().dropna()
    vol_30d = float(returns.tail(30).std() * (252 ** 0.5)) if len(returns) >= 30 else None
    vol_90d = float(returns.tail(90).std() * (252 ** 0.5)) if len(returns) >= 90 else None
    avg_daily_move = float(returns.tail(21).abs().mean()) if len(returns) >= 21 else None

    rolling_max = close.cummax()
    drawdown = (close - rolling_max) / rolling_max
    max_dd_1y = float(drawdown.min()) if not drawdown.empty else None

    vol_today = float(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else None
    vol_avg = float(hist["Volume"].tail(20).mean()) if "Volume" in hist.columns else None
    vol_ratio = (vol_today / vol_avg) if (vol_today and vol_avg and vol_avg > 0) else None

    return {
        "price": price,
        "sma20": sma20, "sma50": sma50, "sma200": sma200,
        "vs_sma20": vs_ma(sma20), "vs_sma50": vs_ma(sma50), "vs_sma200": vs_ma(sma200),
        "rsi": rsi,
        "macd_hist": macd_hist, "macd_signal": macd_sig,
        "macd_bullish": macd_hist > 0,
        "bb": bb, "atr": atr,
        "atr_pct": atr / price if price > 0 else None,
        "perf_1w": _perf(close, 5),
        "perf_1m": _perf(close, 21),
        "perf_3m": _perf(close, 63),
        "perf_6m": _perf(close, 126),
        "perf_ytd": _ytd_perf(hist),
        "vol_30d_ann": vol_30d,
        "vol_90d_ann": vol_90d,
        "avg_daily_move": avg_daily_move,
        "max_drawdown_1y": max_dd_1y,
        "volume_today": vol_today,
        "volume_avg_20d": vol_avg,
        "volume_ratio": vol_ratio,
    }


def _ytd_perf(hist: pd.DataFrame) -> float | None:
    try:
        # Use .year attribute to avoid tz-aware vs naive comparison errors
        year = datetime.now().year
        mask = hist.index.year >= year
        ytd = hist[mask]
        if ytd.empty or len(ytd) < 2:
            return None
        start = float(ytd["Close"].iloc[0])
        end = float(ytd["Close"].iloc[-1])
        return (end - start) / start if start > 0 else None
    except Exception:
        return None


def _relative_perf(ticker_hist: pd.DataFrame, spy_hist: pd.DataFrame,
                   n: int) -> float | None:
    t = _perf(ticker_hist["Close"].astype(float), n)
    s = _perf(spy_hist["Close"].astype(float), n)
    if t is None or s is None:
        return None
    return t - s


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def _gather_data(
    symbol: str,
    spy_hist: pd.DataFrame | None = None,
    as_of_date=None,
) -> dict:
    """Fetch all yfinance data for one ticker and compute indicators.

    When `as_of_date` is provided (backtest mode), daily history is sliced to
    bars on or before that date so no future prices leak into the indicators.
    Note: `info` fields always reflect the current snapshot — yfinance does not
    provide historical fundamentals.
    """
    tk = yf.Ticker(symbol)

    try:
        info = tk.info or {}
    except Exception as e:
        log.warning(f"{symbol}: yf.info failed: {e}")
        info = {}

    try:
        period = "2y" if as_of_date is not None else "1y"
        hist = tk.history(period=period, interval="1d", auto_adjust=True)
        if as_of_date is not None and not hist.empty:
            # Slice to bars on or before as_of_date (no lookahead)
            cutoff = pd.Timestamp(as_of_date)
            idx = hist.index
            if idx.tz is not None:
                cutoff = cutoff.tz_localize(idx.tz) if cutoff.tz is None else cutoff.tz_convert(idx.tz)
            hist = hist[idx.normalize() <= cutoff.normalize()]
    except Exception as e:
        log.warning(f"{symbol}: yf.history failed: {e}")
        hist = pd.DataFrame()

    indicators = _compute_indicators(hist) if not hist.empty else {}

    # Relative strength vs SPY
    rel_1m = rel_3m = None
    if spy_hist is not None and not hist.empty:
        rel_1m = _relative_perf(hist, spy_hist, 21)
        rel_3m = _relative_perf(hist, spy_hist, 63)
    indicators["rel_vs_spy_1m"] = rel_1m
    indicators["rel_vs_spy_3m"] = rel_3m

    # News headlines — date-filtered in backtest mode
    headlines = _fetch_headlines(symbol, as_of_date=as_of_date)
    _seen_titles = set()
    _deduped = []
    for _h in headlines:
        _key = " ".join(_h.lower().split())[:100]
        if _key and _key not in _seen_titles:
            _seen_titles.add(_key)
            _deduped.append(_h)
    headlines = _deduped

    # Analyst recommendations — filter to as_of_date in backtest mode so we
    # don't use upgrades/downgrades that hadn't happened yet on the sim date.
    if as_of_date is not None:
        analyst_summary = _analyst_summary_as_of(tk, as_of_date)
    else:
        analyst_summary = _analyst_summary(tk)

    # Insider transactions
    insider_summary = _insider_summary(tk)

    # Next earnings date
    next_earnings = _next_earnings(tk)

    # Finnhub historical fundamentals overlay (backtest mode only).
    # yfinance .info is always the current snapshot; when as_of_date is set,
    # replace the key financial fields with historically accurate values from
    # Finnhub so the deep scorer sees the right numbers for the simulation date.
    if as_of_date is not None:
        try:
            from ..data.finnhub_fundamentals import get_historical_financials
            fh = get_historical_financials(symbol, as_of_date)
            if fh:
                if fh.get("revenue") is not None:
                    info["totalRevenue"] = fh["revenue"]
                if fh.get("gross_margin") is not None:
                    info["grossMargins"] = fh["gross_margin"]
                if fh.get("net_margin") is not None:
                    info["profitMargins"] = fh["net_margin"]
                if fh.get("debt_to_equity") is not None:
                    info["debtToEquity"] = fh["debt_to_equity"]
                if fh.get("op_income") is not None:
                    info["operatingIncome"] = fh["op_income"]
                log.debug(f"{symbol}: Finnhub overlay applied for {as_of_date}")
        except Exception as e:
            log.debug(f"{symbol}: Finnhub overlay skipped: {e}")

    return {
        "symbol": symbol,
        "info": info,
        "indicators": indicators,
        "headlines": headlines,
        "analyst_summary": analyst_summary,
        "insider_summary": insider_summary,
        "next_earnings": next_earnings,
    }


def _fetch_headlines(symbol: str, limit: int = 10, as_of_date=None) -> list[str]:
    """Fetch news headlines. When as_of_date is set (backtest), filter to [as_of_date-30d, as_of_date]."""
    try:
        cfg = load_config()
        newsapi_key = cfg.get("secrets", {}).get("newsapi_key", "")
        if newsapi_key:
            import requests
            from datetime import timedelta
            if as_of_date is not None:
                end_dt = datetime.combine(as_of_date, datetime.max.time()) if not isinstance(as_of_date, datetime) else as_of_date
                start_dt = end_dt - timedelta(days=30)
                params = {
                    "q": symbol,
                    "from": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "to": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "sortBy": "publishedAt", "language": "en",
                    "pageSize": limit, "apiKey": newsapi_key,
                }
            else:
                cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
                params = {"q": symbol, "from": cutoff, "sortBy": "publishedAt",
                          "language": "en", "pageSize": limit, "apiKey": newsapi_key}
            r = requests.get("https://newsapi.org/v2/everything", params=params, timeout=8)
            if r.ok:
                return [a["title"] for a in r.json().get("articles", [])[:limit]]
    except Exception:
        pass

    try:
        news = yf.Ticker(symbol).news or []
        return [n.get("title", "") for n in news[:limit] if n.get("title")]
    except Exception:
        return []


def _analyst_summary(tk: yf.Ticker) -> dict:
    try:
        recs = tk.recommendations
        if recs is not None and not recs.empty:
            recent = recs.tail(6)
            cols = [c for c in ["strongBuy", "buy", "hold", "sell", "strongSell"] if c in recent.columns]
            if cols:
                totals = recent[cols].sum()
                bull = int(totals.get("strongBuy", 0) + totals.get("buy", 0))
                bear = int(totals.get("sell", 0) + totals.get("strongSell", 0))
                return {"recent_bull_ratings": bull, "recent_bear_ratings": bear,
                        "recent_hold_ratings": int(totals.get("hold", 0))}
    except Exception:
        pass
    return {}


def _analyst_summary_as_of(tk: yf.Ticker, as_of_date) -> dict:
    """Like _analyst_summary but only considers ratings issued on or before as_of_date."""
    try:
        recs = tk.recommendations
        if recs is not None and not recs.empty:
            cutoff = pd.Timestamp(as_of_date)
            idx = recs.index
            if idx.tz is not None:
                cutoff = cutoff.tz_localize(idx.tz) if cutoff.tz is None else cutoff.tz_convert(idx.tz)
            hist_recs = recs[idx.normalize() <= cutoff.normalize()]
            if not hist_recs.empty:
                recent = hist_recs.tail(6)
                cols = [c for c in ["strongBuy", "buy", "hold", "sell", "strongSell"] if c in recent.columns]
                if cols:
                    totals = recent[cols].sum()
                    bull = int(totals.get("strongBuy", 0) + totals.get("buy", 0))
                    bear = int(totals.get("sell", 0) + totals.get("strongSell", 0))
                    return {"recent_bull_ratings": bull, "recent_bear_ratings": bear,
                            "recent_hold_ratings": int(totals.get("hold", 0))}
    except Exception:
        pass
    return {}


def _insider_summary(tk: yf.Ticker) -> dict:
    try:
        txns = tk.insider_transactions
        if txns is not None and not txns.empty:
            buys = sells = 0
            for _, row in txns.iterrows():
                text = str(row.get("Text", "") or row.get("Transaction", "")).lower()
                if "sale" in text or "sell" in text:
                    sells += 1
                elif "purchase" in text or "buy" in text or "acquisition" in text:
                    buys += 1
            return {"insider_buys_12m": buys, "insider_sells_12m": sells,
                    "net_insider": "bullish" if buys > sells else
                                  ("bearish" if sells > buys else "neutral")}
    except Exception:
        pass
    return {}


def _next_earnings(tk: yf.Ticker) -> str | None:
    try:
        cal = tk.calendar
        if cal is not None and not cal.empty:
            if "Earnings Date" in cal.index:
                val = cal.loc["Earnings Date"].iloc[0]
                # Normalize to plain date string regardless of tz-awareness
                ts = pd.Timestamp(val)
                return ts.strftime("%Y-%m-%d")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Prompt builders — each formats data as human-readable text for the LLM
# ---------------------------------------------------------------------------

def _technical_prompt(symbol: str, info: dict, ind: dict) -> str:
    price = ind.get("price") or _safe(info.get("currentPrice")) or _safe(info.get("regularMarketPrice"))
    high52 = _safe(info.get("fiftyTwoWeekHigh"))
    low52 = _safe(info.get("fiftyTwoWeekLow"))
    pct_off_high = f"{(price - high52) / high52 * 100:.1f}%" if (price and high52 and high52 > 0) else "n/a"
    pct_in_range = (
        f"{(price - low52) / (high52 - low52) * 100:.0f}% of 52W range"
        if (price and high52 and low52 and (high52 - low52) > 0) else "n/a"
    )
    bb = ind.get("bb") or {}

    def ma_line(label, sma, vs):
        if sma is None:
            return f"  {label}: n/a"
        arrow = "ABOVE ↑" if vs and vs > 0 else "BELOW ↓"
        return f"  {label}: ${sma:.2f} ({_pct(vs)} — price {arrow})"

    lines = [
        f"## Technical Data: {symbol}",
        f"Price: ${price:.2f}" if price else "Price: n/a",
        f"52W High: ${high52:.2f} | 52W Low: ${low52:.2f} | Off High: {pct_off_high} | {pct_in_range}" if high52 else "",
        "",
        "Moving Averages:",
        ma_line("20-day SMA", ind.get("sma20"), ind.get("vs_sma20")),
        ma_line("50-day SMA", ind.get("sma50"), ind.get("vs_sma50")),
        ma_line("200-day SMA", ind.get("sma200"), ind.get("vs_sma200")),
        "",
        f"RSI (14): {_num(ind.get('rsi'))} — {'oversold (<30)' if (ind.get('rsi') or 50) < 30 else 'overbought (>70)' if (ind.get('rsi') or 50) > 70 else 'neutral'}",
        f"MACD Histogram: {_num(ind.get('macd_hist'), 3)} ({'bullish crossover' if ind.get('macd_bullish') else 'bearish crossover'})",
        f"Bollinger Band position: {_num(bb.get('position'))} (0=lower band, 1=upper band)",
        f"ATR (14-day): ${_num(ind.get('atr'))} ({_pct(ind.get('atr_pct'))} of price daily volatility)",
        "",
        "Performance:",
        f"  1 Week: {_pct(ind.get('perf_1w'))}",
        f"  1 Month: {_pct(ind.get('perf_1m'))}  | vs SPY: {_pct(ind.get('rel_vs_spy_1m'))}",
        f"  3 Month: {_pct(ind.get('perf_3m'))}  | vs SPY: {_pct(ind.get('rel_vs_spy_3m'))}",
        f"  6 Month: {_pct(ind.get('perf_6m'))}",
        f"  YTD:     {_pct(ind.get('perf_ytd'))}",
        "",
        f"Volume today: {_usd(ind.get('volume_today'), 'n/a')} shares | Avg 20d: {_usd(ind.get('volume_avg_20d'), 'n/a')} | Ratio: {_num(ind.get('volume_ratio'))}x",
        f"Beta: {_num(_safe(info.get('beta')))}",
        f"30d Historical Volatility (annualized): {_pct(ind.get('vol_30d_ann'))}",
        f"Max Drawdown (1 year): {_pct(ind.get('max_drawdown_1y'))}",
    ]
    data_block = "\n".join(l for l in lines if l is not None)

    return (
        f"{data_block}\n\n"
        "Score the TECHNICAL quality of this stock 0-100 based on:\n"
        "trend (price vs MAs), momentum (RSI, MACD), volume pattern, "
        "relative strength vs SPY, position in 52W range, and volatility.\n\n"
        "Higher = stronger technical setup. 50 = neutral. "
        "Reply ONLY with JSON:\n"
        '{"score": <int 0-100>, "rationale": "<30 words>", '
        '"bull": "<top technical bull signal>", "bear": "<top technical bear signal>"}'
    )


def _fundamental_prompt(symbol: str, info: dict) -> str:
    name = _safe(info.get("longName"), symbol)
    sector = _safe(info.get("sector"), "Unknown")
    industry = _safe(info.get("industry"), "Unknown")
    summary = (_safe(info.get("longBusinessSummary"), "") or "")[:300]

    mktcap = _safe(info.get("marketCap"))
    tier = ("Large Cap" if mktcap and mktcap >= 10e9 else
            "Mid Cap" if mktcap and mktcap >= 2e9 else
            "Small Cap" if mktcap and mktcap >= 300e6 else
            "Micro Cap" if mktcap else "Unknown")

    cash = _safe(info.get("totalCash"))
    debt = _safe(info.get("totalDebt"))
    net_cash = (cash - debt) if (cash is not None and debt is not None) else None

    fcf = _safe(info.get("freeCashflow"))
    cash_runway = None
    if fcf and fcf < 0 and cash and cash > 0:
        monthly_burn = abs(fcf) / 12
        cash_runway = cash / monthly_burn if monthly_burn > 0 else None

    lines = [
        f"## Fundamental Data: {symbol} — {name}",
        f"Sector: {sector} | Industry: {industry} | Size: {tier}",
        f"Business: {summary}",
        "",
        "Financials (TTM):",
        f"  Market Cap: {_usd(mktcap)}",
        f"  Enterprise Value: {_usd(_safe(info.get('enterpriseValue')))}",
        f"  Revenue: {_usd(_safe(info.get('totalRevenue')))}",
        f"  Revenue Growth YoY: {_pct(_safe(info.get('revenueGrowth')))}",
        f"  Gross Margin: {_pct(_safe(info.get('grossMargins')))}",
        f"  Operating Margin: {_pct(_safe(info.get('operatingMargins')))}",
        f"  Net Margin: {_pct(_safe(info.get('profitMargins')))}",
        f"  EBITDA: {_usd(_safe(info.get('ebitda')))}",
        f"  Free Cash Flow: {_usd(fcf)}",
        f"  EPS (trailing): {_num(_safe(info.get('trailingEps')))}",
        "",
        "Balance Sheet:",
        f"  Cash: {_usd(cash)}",
        f"  Total Debt: {_usd(debt)}",
        f"  Net Cash: {_usd(net_cash)}",
        f"  Cash Runway: {'~' + _num(cash_runway, 0) + ' months' if cash_runway else 'n/a (FCF positive or unknown)'}",
        f"  Debt/Equity: {_num(_safe(info.get('debtToEquity')))}",
        f"  Current Ratio: {_num(_safe(info.get('currentRatio')))}",
        f"  Quick Ratio: {_num(_safe(info.get('quickRatio')))}",
        "",
        "Valuation:",
        f"  P/E (trailing): {_num(_safe(info.get('trailingPE')))}",
        f"  P/E (forward):  {_num(_safe(info.get('forwardPE')))}",
        f"  PEG Ratio:      {_num(_safe(info.get('pegRatio')))}",
        f"  P/S (trailing): {_num(_safe(info.get('priceToSalesTrailing12Months')))}",
        f"  P/B Ratio:      {_num(_safe(info.get('priceToBook')))}",
        f"  EV/Revenue:     {_num(_safe(info.get('enterpriseToRevenue')))}",
        f"  EV/EBITDA:      {_num(_safe(info.get('enterpriseToEbitda')))}",
        "",
        "Growth & Profitability:",
        f"  Earnings Growth (YoY): {_pct(_safe(info.get('earningsGrowth')))}",
        f"  Quarterly Earnings Growth: {_pct(_safe(info.get('earningsQuarterlyGrowth')))}",
        f"  Return on Equity: {_pct(_safe(info.get('returnOnEquity')))}",
        f"  Return on Assets: {_pct(_safe(info.get('returnOnAssets')))}",
        f"  Full-time Employees: {_safe(info.get('fullTimeEmployees'), 'n/a')}",
    ]
    data_block = "\n".join(l for l in lines if l is not None)

    return (
        f"{data_block}\n\n"
        "Score the FUNDAMENTAL quality of this stock 0-100 based on:\n"
        "revenue growth, margins, balance sheet strength, valuation relative "
        "to growth (PEG / P/S vs growth rate), cash position, and profitability.\n\n"
        "Higher = stronger fundamentals. 50 = average. "
        "Reply ONLY with JSON:\n"
        '{"score": <int 0-100>, "rationale": "<30 words>", '
        '"bull": "<top fundamental bull point>", "bear": "<top fundamental bear point>"}'
    )


def _sentiment_prompt(symbol: str, info: dict, headlines: list[str],
                      analyst_summary: dict, insider_summary: dict) -> str:
    price = _safe(info.get("currentPrice")) or _safe(info.get("regularMarketPrice"))
    target = _safe(info.get("targetMeanPrice"))
    upside = f"{(target - price) / price * 100:.1f}%" if (target and price and price > 0) else "n/a"

    rec_mean = _safe(info.get("recommendationMean"))
    rec_label = _safe(info.get("recommendationKey"), "n/a")
    rec_interp = (
        "Strong Buy (1.0-1.5)" if rec_mean and rec_mean <= 1.5 else
        "Buy (1.5-2.5)" if rec_mean and rec_mean <= 2.5 else
        "Hold (2.5-3.5)" if rec_mean and rec_mean <= 3.5 else
        "Underperform (3.5-4.5)" if rec_mean and rec_mean <= 4.5 else
        "Sell (4.5-5.0)" if rec_mean else "n/a"
    )

    short_float = _safe(info.get("shortPercentOfFloat"))
    short_ratio = _safe(info.get("shortRatio"))

    news_block = "\n".join(f"  - {h}" for h in headlines[:8]) if headlines else "  (no recent headlines found)"

    bull_r = analyst_summary.get("recent_bull_ratings", "n/a")
    bear_r = analyst_summary.get("recent_bear_ratings", "n/a")
    hold_r = analyst_summary.get("recent_hold_ratings", "n/a")
    ins_buys = insider_summary.get("insider_buys_12m", "n/a")
    ins_sells = insider_summary.get("insider_sells_12m", "n/a")
    ins_net = insider_summary.get("net_insider", "n/a")

    lines = [
        f"## Sentiment Data: {symbol}",
        "",
        "Recent Headlines (last 30 days):",
        news_block,
        "",
        "Analyst Ratings:",
        f"  Consensus: {rec_label} | Mean score: {_num(rec_mean)} ({rec_interp})",
        f"  # of Analysts: {_safe(info.get('numberOfAnalystOpinions'), 'n/a')}",
        f"  Price Target — Mean: {_usd(target)} | High: {_usd(_safe(info.get('targetHighPrice')))} | Low: {_usd(_safe(info.get('targetLowPrice')))}",
        f"  Upside to Mean PT from current price: {upside}",
        f"  Recent Ratings (last 6 periods): {bull_r} bull / {hold_r} hold / {bear_r} bear",
        "",
        "Short Interest:",
        f"  Short % of Float: {_pct(short_float)}",
        f"  Short Ratio (days to cover): {_num(short_ratio)}",
        f"  Squeeze potential: {'HIGH (>20% float short)' if short_float and short_float > 0.20 else 'MODERATE (10-20%)' if short_float and short_float > 0.10 else 'LOW (<10%)'}",
        "",
        "Ownership:",
        f"  Institutional: {_pct(_safe(info.get('institutionPercentHeld')))}",
        f"  Insider: {_pct(_safe(info.get('insiderPercentHeld')))}",
        "",
        "Insider Transactions (12 months):",
        f"  Buys: {ins_buys} | Sells: {ins_sells} | Net sentiment: {ins_net}",
    ]
    data_block = "\n".join(l for l in lines if l is not None)

    return (
        f"{data_block}\n\n"
        "Score SENTIMENT & MOMENTUM 0-100 based on:\n"
        "news tone, analyst consensus and PT upside, short squeeze potential, "
        "institutional interest, and insider behavior.\n\n"
        "Higher = more bullish sentiment. 50 = mixed/neutral. "
        "Reply ONLY with JSON:\n"
        '{"score": <int 0-100>, "rationale": "<30 words>", '
        '"bull": "<top bullish sentiment signal>", "bear": "<top bearish sentiment signal>"}'
    )


def _risk_prompt(symbol: str, info: dict, ind: dict, next_earnings: str | None) -> str:
    price = ind.get("price") or _safe(info.get("currentPrice"))
    high52 = _safe(info.get("fiftyTwoWeekHigh"))
    low52 = _safe(info.get("fiftyTwoWeekLow"))
    pct_off_high = (price - high52) / high52 if (price and high52 and high52 > 0) else None
    pct_in_range = (price - low52) / (high52 - low52) if (price and high52 and low52 and (high52 - low52) > 0) else None

    cash = _safe(info.get("totalCash"))
    debt = _safe(info.get("totalDebt"))
    fcf = _safe(info.get("freeCashflow"))
    cash_runway = None
    if fcf and fcf < 0 and cash and cash > 0:
        monthly_burn = abs(fcf) / 12
        cash_runway = cash / monthly_burn if monthly_burn > 0 else None

    shares_out = _safe(info.get("sharesOutstanding"))
    float_shares = _safe(info.get("floatShares"))
    shares_short = _safe(info.get("sharesShort"))
    short_float = _safe(info.get("shortPercentOfFloat"))

    lines = [
        f"## Risk Data: {symbol}",
        "",
        "Volatility & Price Range:",
        f"  Beta (vs S&P 500): {_num(_safe(info.get('beta')))}",
        f"  52W High: {_usd(high52)} | 52W Low: {_usd(low52)}",
        f"  Current % off 52W High: {_pct(pct_off_high)}",
        f"  Position in 52W Range: {_pct(pct_in_range)} (0%=at low, 100%=at high)",
        f"  30d Annualized Volatility: {_pct(ind.get('vol_30d_ann'))}",
        f"  90d Annualized Volatility: {_pct(ind.get('vol_90d_ann'))}",
        f"  Avg Daily Move: {_pct(ind.get('avg_daily_move'))}",
        f"  Max Drawdown (1 year): {_pct(ind.get('max_drawdown_1y'))}",
        f"  ATR (14d) as % of price: {_pct(ind.get('atr_pct'))}",
        "",
        "Liquidity:",
        f"  Market Cap: {_usd(_safe(info.get('marketCap')))}",
        f"  Avg Daily Volume (3m): {_usd(_safe(info.get('averageVolume')))} shares",
        f"  Float Shares: {_usd(float_shares)}",
        "",
        "Financial Risk:",
        f"  Cash: {_usd(cash)} | Total Debt: {_usd(debt)}",
        f"  Free Cash Flow: {_usd(fcf)}",
        f"  Cash Runway: {'~' + _num(cash_runway, 0) + ' months' if cash_runway else 'FCF positive or not applicable'}",
        f"  Debt/Equity: {_num(_safe(info.get('debtToEquity')))}",
        f"  Current Ratio: {_num(_safe(info.get('currentRatio')))}",
        "",
        "Short & Dilution Risk:",
        f"  Short % of Float: {_pct(short_float)}",
        f"  Short Ratio (days to cover): {_num(_safe(info.get('shortRatio')))}",
        f"  Shares Outstanding: {_usd(shares_out)}",
        f"  Float: {_usd(float_shares)}",
        "",
        f"Next Earnings: {next_earnings or 'unknown'} (binary event risk)",
    ]
    data_block = "\n".join(l for l in lines if l is not None)

    return (
        f"{data_block}\n\n"
        "Score RISK PROFILE 0-100 where 100 = LOWEST risk (safest) and 0 = HIGHEST risk.\n"
        "Consider: beta/volatility, drawdown potential, liquidity, cash runway, "
        "debt burden, short interest crash risk, earnings binary risk.\n\n"
        "Reply ONLY with JSON:\n"
        '{"score": <int 0-100>, "rationale": "<30 words>", '
        '"bull": "<main risk mitigant>", "bear": "<biggest risk factor>"}'
    )


def _thesis_prompt(symbol: str, info: dict, ind: dict,
                   scores: dict[str, dict]) -> str:
    name = _safe(info.get("longName"), symbol)
    sector = _safe(info.get("sector"), "Unknown")
    price = ind.get("price") or _safe(info.get("currentPrice"))
    mktcap = _safe(info.get("marketCap"))

    target = _safe(info.get("targetMeanPrice"))
    upside = f"{(target - price) / price * 100:.1f}%" if (target and price and price > 0) else "n/a"

    tech = scores.get("technical", {})
    fund = scores.get("fundamental", {})
    sent = scores.get("sentiment", {})
    risk = scores.get("risk", {})

    sub_block = "\n".join([
        f"  Technical  (25%): {tech.get('score', 'n/a')}/100 — {tech.get('rationale', '')}",
        f"  Fundamental(25%): {fund.get('score', 'n/a')}/100 — {fund.get('rationale', '')}",
        f"  Sentiment  (20%): {sent.get('score', 'n/a')}/100 — {sent.get('rationale', '')}",
        f"  Risk       (15%): {risk.get('score', 'n/a')}/100 — {risk.get('rationale', '')}",
    ])

    lines = [
        f"## Investment Thesis Synthesis: {symbol} — {name}",
        f"Sector: {sector} | Market Cap: {_usd(mktcap)} | Price: ${price:.2f}" if price else "",
        f"Analyst Mean PT: {_usd(target)} ({upside} upside)" if target else "",
        "",
        "Sub-scores from the other four dimensions:",
        sub_block,
        "",
        "Bull signals:",
        f"  Technical: {tech.get('bull', 'n/a')}",
        f"  Fundamental: {fund.get('bull', 'n/a')}",
        f"  Sentiment: {sent.get('bull', 'n/a')}",
        f"  Risk mitigant: {risk.get('bull', 'n/a')}",
        "",
        "Bear signals:",
        f"  Technical: {tech.get('bear', 'n/a')}",
        f"  Fundamental: {fund.get('bear', 'n/a')}",
        f"  Sentiment: {sent.get('bear', 'n/a')}",
        f"  Key risk: {risk.get('bear', 'n/a')}",
    ]
    data_block = "\n".join(l for l in lines if l is not None)

    return (
        f"{data_block}\n\n"
        "Score THESIS CONVICTION 0-100: how clear, credible, and asymmetric "
        "is the investment case given the data above?\n"
        "Consider: catalyst clarity, bull/bear asymmetry, quality of the edge, "
        "alignment across dimensions.\n\n"
        "Reply ONLY with JSON:\n"
        '{"score": <int 0-100>, "rationale": "<30 words>", '
        '"bull": "<core bull thesis in one line>", "bear": "<core bear risk in one line>"}'
    )


# ---------------------------------------------------------------------------
# LLM scoring
# ---------------------------------------------------------------------------

def _llm_score(prompt: str, max_tokens: int = 500, system: str | None = None) -> dict:
    """Call the configured LLM and return a parsed score dict."""
    try:
        text = chat(prompt=prompt, system=system, max_tokens=max_tokens,
                    temperature=0.1, json_mode=True, tag="deep_scorer")
        logging.debug("deep_scorer _llm_score raw response: %.300s", text)
        data = extract_json_object(text)
        if data is None or "score" not in data:
            logging.warning("deep_scorer: failed to parse LLM score - response: %.500s", text)
            return {"score": 50, "rationale": "parse failed", "bull": "", "bear": ""}
        score = max(0, min(100, int(data.get("score", 50))))
        return {
            "score": score,
            "rationale": str(data.get("rationale", ""))[:200],
            "bull": str(data.get("bull", ""))[:200],
            "bear": str(data.get("bear", ""))[:200],
        }
    except Exception as e:
        log.warning(f"LLM scoring call failed: {e}")
        return {"score": 50, "rationale": f"LLM error: {str(e)[:80]}",
                "bull": "n/a", "bear": "n/a"}


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

def _composite(breakdown: dict[str, dict]) -> tuple[float, str, str]:
    weighted = 0.0
    total_w = 0.0
    for dim, weight in _WEIGHTS.items():
        if dim not in breakdown:
            continue
        try:
            dim_score = float(breakdown[dim]["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if not pd.notna(dim_score):
            continue
        weighted += weight * dim_score
        total_w += weight
    score = weighted / total_w if total_w > 0 else 50.0
    score = max(0.0, min(100.0, score))
    score = round(score, 1)
    letter, signal = _grade(score)
    return score, letter, signal


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_ticker(
    symbol: str,
    spy_hist: pd.DataFrame | None = None,
    as_of_date=None,
) -> dict:
    """Score one ticker across all 5 dimensions. Returns full result dict.

    `as_of_date`: if provided, history is sliced to that date (backtest mode).
    News is date-filtered and LLM prompts include a temporal guard prefix.
    """
    ok, why = llm_available()
    if not ok:
        log.warning(f"deep_scorer: LLM not available ({why}), skipping {symbol}")
        return _unavailable(symbol, why)

    cfg = load_config()
    mt = int(cfg.get("deep_score", {}).get("llm_max_tokens_per_dimension", 500))

    date_str = None
    temporal_system = None
    if as_of_date is not None:
        if isinstance(as_of_date, datetime):
            date_str = as_of_date.strftime("%Y-%m-%d")
        elif hasattr(as_of_date, "strftime"):
            date_str = as_of_date.strftime("%Y-%m-%d")
        else:
            date_str = str(as_of_date)
        temporal_system = (
            f"IMPORTANT — BACKTEST CONTEXT: You are scoring this stock AS OF {date_str}. "
            f"Do NOT reference any price moves, earnings, news, or analyst actions that "
            f"occurred after {date_str}. Base your scores only on the data provided.\n"
        )

    log.info(f"[deep_score] scoring {symbol}{'  (as_of=' + date_str + ')' if date_str else ''}…")
    try:
        data = _gather_data(symbol, spy_hist, as_of_date=as_of_date)
    except Exception as e:
        log.error(f"[deep_score] data gather failed for {symbol}: {e}")
        return _unavailable(symbol, str(e))

    # ETFs and indices have no fundamentals — skip them rather than
    # producing meaningless scores.  They are used for breadth signals,
    # not as individual stock picks.
    quote_type = str(data["info"].get("quoteType", "")).upper()
    if quote_type in ("ETF", "INDEX", "MUTUALFUND"):
        log.info(f"[deep_score] {symbol}: skipping ({quote_type}) — no fundamentals")
        return _unavailable(symbol, f"skipped: {quote_type}")

    info = data["info"]
    ind = data["indicators"]
    headlines = data["headlines"]
    analyst_summary = data["analyst_summary"]
    insider_summary = data["insider_summary"]
    next_earnings = data["next_earnings"]

    if not ind:
        log.warning(f"[deep_score] no price history for {symbol}, skipping")
        return _unavailable(symbol, "no price history")

    breakdown: dict[str, dict] = {}

    log.debug(f"[deep_score] {symbol}: scoring technical…")
    breakdown["technical"] = _llm_score(
        _technical_prompt(symbol, info, ind), mt, system=temporal_system)

    log.debug(f"[deep_score] {symbol}: scoring fundamental…")
    breakdown["fundamental"] = _llm_score(
        _fundamental_prompt(symbol, info), mt, system=temporal_system)

    log.debug(f"[deep_score] {symbol}: scoring sentiment…")
    breakdown["sentiment"] = _llm_score(
        _sentiment_prompt(symbol, info, headlines, analyst_summary, insider_summary),
        mt, system=temporal_system)

    log.debug(f"[deep_score] {symbol}: scoring risk…")
    breakdown["risk"] = _llm_score(
        _risk_prompt(symbol, info, ind, next_earnings), mt, system=temporal_system)

    log.debug(f"[deep_score] {symbol}: scoring thesis…")
    breakdown["thesis"] = _llm_score(
        _thesis_prompt(symbol, info, ind, breakdown), mt, system=temporal_system)

    composite, letter, signal = _composite(breakdown)
    price = ind.get("price") or _safe(info.get("currentPrice"))

    result = {
        "symbol": symbol,
        "score": composite,
        "grade": letter,
        "signal": signal,
        "updated": datetime.now(timezone.utc).isoformat(),
        "price_at_score": float(price) if price else None,
        "next_earnings": next_earnings,
        "breakdown": breakdown,
        "key_stats": {
            "market_cap": _safe(info.get("marketCap")),
            "revenue_growth": _safe(info.get("revenueGrowth")),
            "gross_margin": _safe(info.get("grossMargins")),
            "operating_margin": _safe(info.get("operatingMargins")),
            "pe_forward": _safe(info.get("forwardPE")),
            "ps_ratio": _safe(info.get("priceToSalesTrailing12Months")),
            "ev_revenue": _safe(info.get("enterpriseToRevenue")),
            "short_float": _safe(info.get("shortPercentOfFloat")),
            "beta": _safe(info.get("beta")),
            "analyst_target": _safe(info.get("targetMeanPrice")),
            "recommendation": _safe(info.get("recommendationKey")),
            "analyst_count": _safe(info.get("numberOfAnalystOpinions")),
            "debt_to_equity": _safe(info.get("debtToEquity")),
            "current_ratio": _safe(info.get("currentRatio")),
            "free_cash_flow": _safe(info.get("freeCashflow")),
        },
    }

    log.info(
        f"[deep_score] {symbol}: {composite:.1f}/100 ({letter} — {signal}) | "
        f"tech={breakdown['technical']['score']} fund={breakdown['fundamental']['score']} "
        f"sent={breakdown['sentiment']['score']} risk={breakdown['risk']['score']} "
        f"thesis={breakdown['thesis']['score']}"
    )
    return result


def score_tickers(symbols: list[str]) -> dict[str, dict]:
    """Score a list of tickers and save to trade_scores.json."""
    cfg = load_config()
    max_tickers = int(cfg.get("deep_score", {}).get("max_tickers_per_run", 20))
    symbols = list(symbols)[:max_tickers]

    # Fetch SPY once for relative performance comparison
    spy_hist: pd.DataFrame | None = None
    try:
        spy_hist = yf.Ticker("SPY").history(period="1y", interval="1d", auto_adjust=True)
    except Exception:
        log.warning("[deep_score] could not fetch SPY history for relative performance")

    existing = load_scores()
    results: dict[str, dict] = {}

    for sym in symbols:
        try:
            result = score_ticker(sym, spy_hist)
            results[sym] = result
        except Exception as e:
            log.error(f"[deep_score] {sym} failed: {e}")
            results[sym] = _unavailable(sym, str(e))

    # Merge with existing scores (keep old scores for tickers not re-scored)
    merged = {**existing, **results}
    save_scores(merged)
    return results


def load_scores() -> dict[str, dict]:
    """Load trade_scores.json. Returns {} if the file doesn't exist yet."""
    path = _scores_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"could not load trade_scores.json: {e}")
        return {}


def save_scores(scores: dict[str, dict]) -> None:
    path = _scores_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scores, indent=2, default=str), encoding="utf-8")
    log.info(f"[deep_score] saved {len(scores)} scores to {path}")


def get_score(symbol: str) -> dict | None:
    """Return the stored score for one ticker, or None if not found / stale."""
    scores = load_scores()
    entry = scores.get(symbol.upper())
    if not entry:
        return None

    cfg = load_config()
    max_stale = int(cfg.get("deep_score", {}).get("max_stale_days", 7))
    updated_str = entry.get("updated", "")
    if updated_str:
        try:
            updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - updated).days
            if age_days > max_stale:
                log.debug(f"[deep_score] {symbol} score is {age_days}d old (> {max_stale}d) — treating as stale")
                return None
        except Exception:
            pass
    return entry


def deep_score_gate(symbol: str) -> tuple[bool, float, str]:
    """Gate function for decision_engine.py.

    Returns (allow_buy, size_multiplier, reason).
      allow_buy=False  → veto the BUY signal entirely
      size_multiplier  → 0.0-1.0 scale applied to computed position size
      reason           → short explanation for the journal
    """
    cfg = load_config()
    ds_cfg = cfg.get("deep_score", {}) or {}

    if not ds_cfg.get("enabled", True):
        return True, 1.0, "deep_score disabled"

    entry = get_score(symbol.upper())
    if entry is None:
        return True, 1.0, "no deep score on file — gate bypassed"

    score = float(entry.get("score", 50))
    grade = entry.get("grade", "?")
    signal = entry.get("signal", "?")

    veto_threshold = float(ds_cfg.get("gate_threshold_veto", 25))
    caution_threshold = float(ds_cfg.get("gate_threshold_caution", 40))

    if score < veto_threshold:
        return False, 0.0, f"deep_score={score:.0f} ({grade}/{signal}) — BUY vetoed (F grade)"

    if score < caution_threshold:
        multiplier = 0.5
        return True, multiplier, (
            f"deep_score={score:.0f} ({grade}/{signal}) — size halved (D grade caution)"
        )

    if score < 55:
        multiplier = 0.75
        return True, multiplier, (
            f"deep_score={score:.0f} ({grade}/{signal}) — size reduced 25% (C grade neutral)"
        )

    return True, 1.0, f"deep_score={score:.0f} ({grade}/{signal}) — full size allowed"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _scores_path() -> Path:
    cfg = load_config()
    data_dir = Path(cfg.get("paths", {}).get("data_dir", "data"))
    return data_dir / "trade_scores.json"


def _unavailable(symbol: str, reason: str) -> dict:
    return {
        "symbol": symbol,
        "score": 50.0,
        "grade": "C",
        "signal": "neutral",
        "updated": datetime.now(timezone.utc).isoformat(),
        "price_at_score": None,
        "next_earnings": None,
        "breakdown": {},
        "key_stats": {},
        "error": reason,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")

    if len(sys.argv) > 1:
        tickers = [s.upper() for s in sys.argv[1:]]
    else:
        # Default: score the current shortlist
        try:
            from ..screener import load_shortlist
            tickers = load_shortlist()
        except Exception:
            cfg = load_config()
            tickers = cfg.get("screener", {}).get("watchlist", [])[:20]

    if not tickers:
        print("No tickers to score. Pass tickers as arguments: python -m src.analysis.deep_scorer BBAI NVDA")
        sys.exit(1)

    print(f"\nDeep-scoring {len(tickers)} ticker(s): {', '.join(tickers)}\n")
    results = score_tickers(tickers)

    print(f"\n{'Ticker':<8} {'Score':>6} {'Grade':>6} {'Signal':<14} {'Key Issue'}")
    print("-" * 75)
    for sym, r in sorted(results.items(), key=lambda x: -x[1].get("score", 0)):
        bd = r.get("breakdown", {})
        thesis = bd.get("thesis", {})
        key = thesis.get("bear", r.get("error", "")) or ""
        print(f"{sym:<8} {r.get('score', 0):>6.1f} {r.get('grade', '?'):>6} "
              f"{r.get('signal', '?'):<14} {key[:45]}")
    print()
