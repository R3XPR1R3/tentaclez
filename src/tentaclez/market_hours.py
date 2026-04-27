from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

NYSE = mcal.get_calendar("NYSE")
NY = ZoneInfo("America/New_York")


def is_market_open(now: datetime | None = None) -> bool:
    """True if NYSE regular session is currently open (handles holidays/half-days)."""
    now = now or datetime.now(tz=NY)
    schedule = NYSE.schedule(start_date=now.date(), end_date=now.date())
    if schedule.empty:
        return False
    open_ts = schedule.iloc[0]["market_open"].to_pydatetime()
    close_ts = schedule.iloc[0]["market_close"].to_pydatetime()
    return open_ts <= now.astimezone(open_ts.tzinfo) <= close_ts


def next_open(now: datetime | None = None) -> datetime:
    now = now or datetime.now(tz=NY)
    schedule = NYSE.schedule(start_date=now.date(), end_date=now.date() + timedelta(days=10))
    for _, row in schedule.iterrows():
        open_ts = row["market_open"].to_pydatetime()
        if open_ts > now.astimezone(open_ts.tzinfo):
            return open_ts
    raise RuntimeError("no upcoming NYSE session in the next 10 days")
