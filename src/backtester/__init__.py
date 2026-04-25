"""Walk-forward backtester — runs the full bot pipeline over the last N trading days.

Uses only data that would have been available at each point in time:
  - Daily/hourly bars sliced to the simulation date (DataCache)
  - NewsAPI queries filtered by date range
  - LLM prompts prefixed with a temporal guard ("as of YYYY-MM-DD")
  - Deep scorer runs on Mondays (simulating Sunday pre-run)
"""
from .engine import run_backtest
from .reporter import generate_report
from .data_cache import DataCache
from .broker import BacktestBroker

__all__ = ["run_backtest", "generate_report", "DataCache", "BacktestBroker"]
