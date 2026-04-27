from __future__ import annotations

from dataclasses import dataclass

from .config import AllocationCfg


@dataclass(slots=True)
class ProfitSplit:
    profit_usd: float
    to_core: float
    to_dividend: float
    to_cash: float

    def fmt(self) -> str:
        return (
            f"Realized profit: ${self.profit_usd:.2f}\n"
            f"  → core ETF:    ${self.to_core:.2f}\n"
            f"  → dividend:    ${self.to_dividend:.2f}\n"
            f"  → cash buffer: ${self.to_cash:.2f}"
        )


def split_profit(profit_usd: float, cfg: AllocationCfg) -> ProfitSplit:
    """Distribute realized profit per allocation policy. Loss → no split."""
    if profit_usd <= 0:
        return ProfitSplit(profit_usd=profit_usd, to_core=0, to_dividend=0, to_cash=0)
    return ProfitSplit(
        profit_usd=profit_usd,
        to_core=round(profit_usd * cfg.reinvest_core_pct / 100, 2),
        to_dividend=round(profit_usd * cfg.reinvest_dividend_pct / 100, 2),
        to_cash=round(profit_usd * cfg.cash_buffer_pct / 100, 2),
    )
