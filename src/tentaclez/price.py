from __future__ import annotations

import asyncio
import time as _time
from dataclasses import dataclass
from typing import Literal

import pandas as pd
import yfinance as yf
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential


Period = Literal["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"]


@dataclass(slots=True)
class PriceSnapshot:
    symbol: str
    price: float
    prev_close: float

    @property
    def change_pct(self) -> float:
        if self.prev_close == 0:
            return 0.0
        return (self.price - self.prev_close) / self.prev_close * 100


class PriceFetcher:
    """yfinance wrapper with TTL cache + retry. Single source of market data for the bot."""

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, PriceSnapshot]] = {}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def _fetch_sync(self, symbol: str) -> PriceSnapshot:
        ticker = yf.Ticker(symbol)
        # fast_info works for live/recent quote and is cheap on the network.
        fi = ticker.fast_info
        price = float(fi.get("last_price") or fi.get("lastPrice") or 0)
        prev = float(fi.get("previous_close") or fi.get("previousClose") or 0)
        if price <= 0:
            # fall back to 1-day history
            hist = ticker.history(period="2d", interval="1d")
            if hist.empty:
                raise RuntimeError(f"no quote for {symbol}")
            price = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else price
        return PriceSnapshot(symbol=symbol.upper(), price=price, prev_close=prev)

    async def get(self, symbol: str) -> PriceSnapshot:
        now = _time.monotonic()
        cached = self._cache.get(symbol.upper())
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        snap = await asyncio.to_thread(self._fetch_sync, symbol)
        self._cache[symbol.upper()] = (now, snap)
        return snap

    async def history(self, symbol: str, period: Period = "1y", interval: str = "1d") -> pd.DataFrame:
        def _sync() -> pd.DataFrame:
            df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
            if df.empty:
                return df
            df = df.rename(columns=str.lower)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df[["open", "high", "low", "close", "volume"]]

        try:
            return await asyncio.to_thread(_sync)
        except Exception as e:
            logger.warning("yfinance history failed for {}: {}", symbol, e)
            return pd.DataFrame()
