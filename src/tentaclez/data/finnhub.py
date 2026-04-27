from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


@dataclass(slots=True)
class Quote:
    symbol: str
    price: float
    prev_close: float
    high: float
    low: float
    open: float

    @property
    def change_pct(self) -> float:
        if self.prev_close == 0:
            return 0.0
        return (self.price - self.prev_close) / self.prev_close * 100


@dataclass(slots=True)
class Dividend:
    symbol: str
    ex_date: date
    pay_date: date | None
    amount: float


class FinnhubClient:
    """Thin async wrapper around the bits of Finnhub we use.

    Free tier: 60 req/min. The bot's 5-minute cadence on a watchlist of ~10
    tickers stays well under that — but we wrap requests in retry-with-backoff
    in case we hit a transient 429.
    """

    BASE = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("FINNHUB_API_KEY is empty — set it in .env")
        self._key = api_key
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    async def _get(self, path: str, **params) -> dict:
        params["token"] = self._key
        r = await self._client.get(f"{self.BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()

    async def quote(self, symbol: str) -> Quote:
        data = await self._get("/quote", symbol=symbol)
        return Quote(
            symbol=symbol.upper(),
            price=float(data.get("c", 0) or 0),
            prev_close=float(data.get("pc", 0) or 0),
            high=float(data.get("h", 0) or 0),
            low=float(data.get("l", 0) or 0),
            open=float(data.get("o", 0) or 0),
        )

    async def dividends(self, symbol: str, frm: date, to: date) -> list[Dividend]:
        data = await self._get(
            "/stock/dividend",
            symbol=symbol,
            **{"from": frm.isoformat(), "to": to.isoformat()},
        )
        out: list[Dividend] = []
        for row in data or []:
            ex = row.get("date")
            if not ex:
                continue
            pay = row.get("payDate") or None
            out.append(
                Dividend(
                    symbol=symbol.upper(),
                    ex_date=date.fromisoformat(ex),
                    pay_date=date.fromisoformat(pay) if pay else None,
                    amount=float(row.get("amount", 0) or 0),
                )
            )
        return out
