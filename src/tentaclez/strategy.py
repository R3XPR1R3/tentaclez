"""Ladder Swing strategy.

Core idea (per spec):

    buy_priceₙ  = last_buy_price × (1 - dip_percent)
    sell_priceₙ = buy_priceₙ × (1 + profit_percent)

Buy on dips, sell each lot at its own target price. State is per-ticker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True, frozen=True)
class StrategyParams:
    dip_percent: float       # fraction, e.g. 0.02 = 2%
    profit_percent: float    # fraction, e.g. 0.04 = 4%
    min_trade_pct: float     # fraction of free cash, e.g. 0.10
    max_trade_pct: float     # fraction of free cash, e.g. 0.20


@dataclass(slots=True, frozen=True)
class OpenLot:
    id: int
    qty: float
    entry_price: float
    target_price: float


@dataclass(slots=True)
class TickerState:
    symbol: str
    current_price: float
    last_buy_price: float | None
    open_lots: list[OpenLot]
    free_cash: float


Action = Literal["BUY", "SELL", "HOLD"]


@dataclass(slots=True)
class LadderSignal:
    symbol: str
    action: Action
    reason: str
    suggested_amount_usd: float = 0.0
    suggested_qty: float = 0.0
    suggested_price: float = 0.0
    expected_profit_usd: float = 0.0
    target_price: float | None = None
    lot_id: int | None = None


def generate_signal(state: TickerState, params: StrategyParams) -> LadderSignal:
    """Return a single BUY/SELL/HOLD recommendation per the ladder rules.

    Sell side wins if any open lot reached its target — locking gains beats
    pyramiding into a runaway. Otherwise check for a dip below the last buy.
    """
    # 1. Sell — close the oldest lot whose target is hit (FIFO-friendly).
    matching_lots = [lot for lot in state.open_lots if state.current_price >= lot.target_price]
    if matching_lots:
        lot = min(matching_lots, key=lambda l: l.id)
        profit = (state.current_price - lot.entry_price) * lot.qty
        roi = (state.current_price / lot.entry_price - 1) * 100
        return LadderSignal(
            symbol=state.symbol,
            action="SELL",
            reason=(
                f"price ${state.current_price:.2f} hit target ${lot.target_price:.2f} "
                f"(entry ${lot.entry_price:.2f}, +{roi:.2f}%)"
            ),
            suggested_qty=lot.qty,
            suggested_price=state.current_price,
            expected_profit_usd=profit,
            lot_id=lot.id,
        )

    # 2. Buy — needs a reference price (the most recent fill).
    if state.last_buy_price is None:
        return LadderSignal(
            symbol=state.symbol,
            action="HOLD",
            reason="no reference price yet — record your first /buy to anchor the ladder",
        )

    threshold = state.last_buy_price * (1 - params.dip_percent)
    if state.current_price > threshold:
        return LadderSignal(
            symbol=state.symbol,
            action="HOLD",
            reason=(
                f"price ${state.current_price:.2f} above dip threshold ${threshold:.2f} "
                f"(last buy ${state.last_buy_price:.2f}, need -{params.dip_percent*100:.1f}%)"
            ),
        )

    # Dip threshold met. Size the order against free cash.
    max_amount = state.free_cash * params.max_trade_pct
    min_amount = state.free_cash * params.min_trade_pct
    if state.free_cash <= 0 or max_amount <= 0:
        return LadderSignal(
            symbol=state.symbol,
            action="HOLD",
            reason=(
                f"dip hit ${state.current_price:.2f} ≤ ${threshold:.2f} but free cash is ${state.free_cash:.2f}"
            ),
        )
    amount = max_amount  # take the upper bound on a real dip
    if amount < min_amount:
        # not enough cash for a meaningful trade
        return LadderSignal(
            symbol=state.symbol,
            action="HOLD",
            reason=(
                f"dip hit but cash too low — would only buy ${amount:.2f}, "
                f"min trade is ${min_amount:.2f}"
            ),
        )

    target = state.current_price * (1 + params.profit_percent)
    drop_pct = (1 - state.current_price / state.last_buy_price) * 100
    return LadderSignal(
        symbol=state.symbol,
        action="BUY",
        reason=(
            f"price ${state.current_price:.2f} dipped {drop_pct:.2f}% from last buy ${state.last_buy_price:.2f} "
            f"(threshold {params.dip_percent*100:.1f}%)"
        ),
        suggested_amount_usd=round(amount, 2),
        suggested_price=state.current_price,
        target_price=round(target, 2),
    )
