"""Market time helpers — everything in America/New_York."""
from __future__ import annotations

import logging
from datetime import date, datetime, time

import pytz

EASTERN = pytz.timezone("America/New_York")

# US market full-day holidays we skip. This is a pragmatic subset — for production
# you'd want a proper market calendar like pandas_market_calendars.
_US_HOLIDAYS_2026 = {
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
}

_US_HOLIDAYS_2027 = {
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),
    date(2027, 5, 31),
    date(2027, 6, 18),
    date(2027, 7, 5),
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),
}

_US_HOLIDAYS = _US_HOLIDAYS_2026 | _US_HOLIDAYS_2027

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
PREMARKET_START = time(4, 0)
AFTERHOURS_END = time(20, 0)


def now_eastern() -> datetime:
    return datetime.now(tz=EASTERN)


def is_trading_day(d: date | None = None) -> bool:
    d = d or now_eastern().date()
    if d.weekday() >= 5:  # Sat/Sun
        return False
    if d.year > 2027:
        logging.warning("market_time: no holidays defined for year %d - result may be inaccurate", d.year)
    return d not in _US_HOLIDAYS


def is_market_open(dt: datetime | None = None) -> bool:
    dt = dt or now_eastern()
    if not is_trading_day(dt.date()):
        return False
    return REGULAR_OPEN <= dt.time() <= REGULAR_CLOSE


def is_pre_market(dt: datetime | None = None) -> bool:
    dt = dt or now_eastern()
    if not is_trading_day(dt.date()):
        return False
    return PREMARKET_START <= dt.time() < REGULAR_OPEN


def today_str() -> str:
    return now_eastern().strftime("%Y-%m-%d")
