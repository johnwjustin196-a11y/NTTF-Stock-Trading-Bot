"""Historical fundamental data from Finnhub for backtest use.

Used by the deep scorer when running with an as_of_date to replace the current
yfinance .info snapshot with historically accurate quarterly financials.

Requires FINNHUB_API_KEY in the environment (.env file). Returns empty dict
if the key is missing or the API call fails — the deep scorer falls back to
the yfinance snapshot in that case.

Free tier: 60 API calls/minute, 30 years of data.
Sign up at https://finnhub.io to get a free key.
"""
from __future__ import annotations

import os
from datetime import date

import requests

_BASE = "https://finnhub.io/api/v1"
_TIMEOUT = 10


def _key() -> str:
    return os.getenv("FINNHUB_API_KEY", "")


def get_historical_financials(symbol: str, as_of_date: date) -> dict:
    """Return key financial metrics from the most recent quarterly report
    filed on or before as_of_date.

    Returns an empty dict on failure or if no Finnhub key is set.
    """
    key = _key()
    if not key:
        return {}
    try:
        resp = requests.get(
            f"{_BASE}/stock/financials-reported",
            params={"symbol": symbol, "freq": "quarterly", "token": key},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        # Only consider reports filed on or before as_of_date
        valid = [r for r in data if _report_date(r) <= as_of_date]
        if not valid:
            return {}
        latest = sorted(valid, key=_report_date)[-1]
        return _parse_financials(latest)
    except Exception:
        return {}


def _report_date(r: dict) -> date:
    filed = r.get("filed", "")
    try:
        return date.fromisoformat(str(filed)[:10])
    except Exception:
        # Fall back to approximate date from year/quarter
        year = r.get("year", 2000)
        quarter = r.get("quarter", 1)
        month = min(quarter * 3, 12)
        return date(int(year), int(month), 1)


def _find(items: list, *labels: str) -> float | None:
    """Search a list of {label, value} items for any matching label."""
    target = {lbl.lower() for lbl in labels}
    for item in items:
        if item.get("label", "").lower() in target:
            v = item.get("value")
            if v is not None:
                return float(v)
    return None


def _parse_financials(r: dict) -> dict:
    """Extract the key metrics used by the deep scorer from a Finnhub report."""
    ic = r.get("report", {}).get("ic", [])  # income statement
    bs = r.get("report", {}).get("bs", [])  # balance sheet
    cf = r.get("report", {}).get("cf", [])  # cash flow

    revenue = _find(ic,
        "Revenue", "Revenues", "Total Revenue", "Net sales", "Net Sales",
        "Net revenues", "Total net revenue", "Net product sales",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
    )
    net_income = _find(ic,
        "Net Income", "Net income", "NetIncomeLoss",
        "Net Income Common Stockholders",
    )
    gross_profit = _find(ic,
        "Gross Profit", "GrossProfit", "Gross margin",
        "Gross Margin",
    )
    op_income = _find(ic,
        "Operating Income", "Operating income", "Operating Income Loss",
        "OperatingIncomeLoss",
    )
    total_assets = _find(bs, "Total Assets", "Assets")
    total_debt = _find(bs, "Total Debt", "Long Term Debt", "LongTermDebt",
                       "LongTermDebtNoncurrent")
    total_equity = _find(bs, "Total Stockholders Equity", "StockholdersEquity",
                         "Stockholders Equity", "Total Equity")
    op_cash = _find(cf, "Operating Cash Flow", "Net Cash from Operations",
                    "NetCashProvidedByUsedInOperatingActivities")

    gross_margin = (
        gross_profit / revenue
        if gross_profit is not None and revenue and revenue != 0
        else None
    )
    net_margin = (
        net_income / revenue
        if net_income is not None and revenue and revenue != 0
        else None
    )
    debt_to_equity = (
        total_debt / total_equity
        if total_debt is not None and total_equity and total_equity != 0
        else None
    )

    return {k: v for k, v in {
        "revenue": revenue,
        "net_income": net_income,
        "gross_profit": gross_profit,
        "op_income": op_income,
        "total_assets": total_assets,
        "total_debt": total_debt,
        "total_equity": total_equity,
        "op_cash_flow": op_cash,
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "debt_to_equity": debt_to_equity,
    }.items() if v is not None}
