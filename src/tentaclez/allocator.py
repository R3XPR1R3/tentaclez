from __future__ import annotations

from dataclasses import dataclass

from .config import AllocationCfg


@dataclass(slots=True)
class ProfitSplit:
    profit_usd: float
    to_trade_pool: float
    to_dividend: float
    dividend_targets: list[str]

    def fmt(self) -> str:
        targets = ", ".join(self.dividend_targets) or "—"
        return (
            f"Realized profit: ${self.profit_usd:.2f}\n"
            f"  → trade pool (cash): ${self.to_trade_pool:.2f}\n"
            f"  → dividend ETFs ({targets}): ${self.to_dividend:.2f}"
        )


def split_profit(profit_usd: float, cfg: AllocationCfg) -> ProfitSplit:
    """Distribute realized profit per allocation policy. Loss → no split."""
    if profit_usd <= 0:
        return ProfitSplit(
            profit_usd=profit_usd,
            to_trade_pool=0,
            to_dividend=0,
            dividend_targets=cfg.dividend_etf_symbols,
        )
    return ProfitSplit(
        profit_usd=profit_usd,
        to_trade_pool=round(profit_usd * cfg.reinvest_trade_pct / 100, 2),
        to_dividend=round(profit_usd * cfg.dividend_etf_pct / 100, 2),
        dividend_targets=cfg.dividend_etf_symbols,
    )
