"""SnapTrade integration — read-only Robinhood portfolio sync.

SnapTrade is a third-party aggregator with an official Robinhood integration.
Two-step flow:

    1. Register a SnapTrade user (once). Bot stores the user_id + user_secret.
    2. Generate a one-shot connection portal URL and send it in Telegram. The
       user taps it, lands on SnapTrade's hosted page, logs in to Robinhood,
       and SnapTrade stores the brokerage link.

After that, accounts/holdings are read via this client without any further
browser hops. Robinhood-via-SnapTrade is read-only — you cannot place orders
through it (limitation on Robinhood's side, not SnapTrade's).

Free tier: 5 connected users, daily-refreshed cached data. Real-time access is
on the paid plan.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass

from loguru import logger

try:
    from snaptrade_client import SnapTrade  # type: ignore
except Exception:  # pragma: no cover - import guarded so the bot still boots
    SnapTrade = None  # type: ignore[assignment]


@dataclass(slots=True)
class BrokerageAccount:
    account_id: str
    name: str
    institution: str
    cash: float


@dataclass(slots=True)
class BrokeragePosition:
    symbol: str
    qty: float
    price: float
    market_value: float
    avg_buy_price: float | None


class SnapTradeClient:
    """Thin async-friendly wrapper around the official SnapTrade SDK.

    The SDK is sync-only, so every call goes through asyncio.to_thread to keep
    the event loop responsive.
    """

    def __init__(self, client_id: str, consumer_key: str) -> None:
        if SnapTrade is None:
            raise RuntimeError(
                "snaptrade-python-sdk not installed. add it via pip / docker rebuild."
            )
        if not client_id or not consumer_key:
            raise ValueError("SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY are required")
        self._sdk = SnapTrade(client_id=client_id, consumer_key=consumer_key)

    @staticmethod
    def make_user_id(prefix: str = "tentaclez") -> str:
        return f"{prefix}-{secrets.token_hex(8)}"

    # ---- registration ----

    async def register_user(self, user_id: str) -> str:
        """Returns user_secret. SnapTrade only issues this once per user_id."""

        def _sync() -> str:
            resp = self._sdk.authentication.register_snap_trade_user(user_id=user_id)
            secret = getattr(resp, "user_secret", None) or resp.get("userSecret")  # type: ignore[union-attr]
            if not secret:
                raise RuntimeError(f"SnapTrade did not return userSecret: {resp!r}")
            return secret

        return await asyncio.to_thread(_sync)

    async def delete_user(self, user_id: str) -> None:
        def _sync() -> None:
            self._sdk.authentication.delete_snap_trade_user(user_id=user_id)

        await asyncio.to_thread(_sync)

    # ---- connection portal ----

    async def login_url(self, user_id: str, user_secret: str) -> str:
        """One-shot URL the user opens in a browser to complete the brokerage link."""

        def _sync() -> str:
            resp = self._sdk.authentication.login_snap_trade_user(
                user_id=user_id, user_secret=user_secret
            )
            url = getattr(resp, "redirect_uri", None) or resp.get("redirectURI")  # type: ignore[union-attr]
            if not url:
                raise RuntimeError(f"SnapTrade did not return a redirect URL: {resp!r}")
            return url

        return await asyncio.to_thread(_sync)

    # ---- accounts / holdings ----

    async def list_accounts(self, user_id: str, user_secret: str) -> list[BrokerageAccount]:
        def _sync() -> list[BrokerageAccount]:
            resp = self._sdk.account_information.list_user_accounts(
                user_id=user_id, user_secret=user_secret
            )
            out: list[BrokerageAccount] = []
            for acc in resp:  # type: ignore[union-attr]
                out.append(
                    BrokerageAccount(
                        account_id=str(getattr(acc, "id", None) or acc["id"]),
                        name=str(getattr(acc, "name", None) or acc.get("name") or "—"),
                        institution=str(
                            getattr(acc, "institution_name", None)
                            or acc.get("institution_name")
                            or "—"
                        ),
                        cash=float(
                            getattr(acc, "cash", None) or acc.get("cash") or 0.0
                        ),
                    )
                )
            return out

        return await asyncio.to_thread(_sync)

    async def get_holdings(
        self, user_id: str, user_secret: str, account_id: str
    ) -> list[BrokeragePosition]:
        def _sync() -> list[BrokeragePosition]:
            resp = self._sdk.account_information.get_user_account_positions(
                user_id=user_id, user_secret=user_secret, account_id=account_id
            )
            out: list[BrokeragePosition] = []
            for pos in resp:  # type: ignore[union-attr]
                # SnapTrade payload nests the symbol under 'symbol.symbol' or 'symbol.raw_symbol'
                sym_obj = getattr(pos, "symbol", None) or pos.get("symbol", {})
                ticker = (
                    getattr(sym_obj, "symbol", None)
                    if hasattr(sym_obj, "symbol")
                    else None
                )
                if ticker is None and isinstance(sym_obj, dict):
                    nested = sym_obj.get("symbol") or {}
                    ticker = (
                        nested.get("symbol")
                        or sym_obj.get("raw_symbol")
                        or sym_obj.get("description")
                    )
                if not ticker:
                    logger.debug("skipping position with no symbol: {}", pos)
                    continue
                qty = float(
                    getattr(pos, "units", None) if hasattr(pos, "units") else pos.get("units", 0)
                ) or 0.0
                price = float(
                    getattr(pos, "price", None) if hasattr(pos, "price") else pos.get("price", 0)
                ) or 0.0
                avg = (
                    getattr(pos, "average_purchase_price", None)
                    if hasattr(pos, "average_purchase_price")
                    else pos.get("average_purchase_price")
                )
                out.append(
                    BrokeragePosition(
                        symbol=str(ticker).upper(),
                        qty=qty,
                        price=price,
                        market_value=qty * price,
                        avg_buy_price=float(avg) if avg is not None else None,
                    )
                )
            return out

        return await asyncio.to_thread(_sync)
