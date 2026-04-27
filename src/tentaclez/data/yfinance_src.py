from __future__ import annotations

import asyncio

import pandas as pd
import yfinance as yf


async def fetch_history(
    symbol: str, period: str = "1y", interval: str = "1d"
) -> pd.DataFrame:
    """yfinance is sync; run it in a thread so we don't block the event loop.

    Returns a DataFrame with columns: open, high, low, close, volume (lowercased).
    """

    def _sync() -> pd.DataFrame:
        df = yf.download(
            symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
        if df.empty:
            return df
        df = df.rename(columns=str.lower)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df[["open", "high", "low", "close", "volume"]]

    return await asyncio.to_thread(_sync)
