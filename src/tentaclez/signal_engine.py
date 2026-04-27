from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import SignalsCfg
from .data.finnhub import Dividend
from .indicators import IndicatorSnapshot


@dataclass(slots=True)
class Signal:
    symbol: str
    action: str  # BUY / SELL / HOLD
    score: float
    reasons: list[str] = field(default_factory=list)
    price: float = 0.0
    stop: float | None = None
    target: float | None = None
    suggested_position_usd: float = 0.0


def _score_rsi(snap: IndicatorSnapshot, cfg) -> tuple[float, str | None]:
    if not cfg.enabled or snap.rsi is None:
        return 0, None
    if snap.rsi <= cfg.oversold:
        return cfg.weight, f"RSI {snap.rsi:.1f} oversold (<{cfg.oversold})"
    if snap.rsi >= cfg.overbought:
        return -cfg.weight, f"RSI {snap.rsi:.1f} overbought (>{cfg.overbought})"
    return 0, None


def _score_macd(snap: IndicatorSnapshot, cfg) -> tuple[float, str | None]:
    if not cfg.enabled or snap.macd is None or snap.macd_signal is None:
        return 0, None
    if snap.macd > snap.macd_signal and (snap.macd_hist or 0) > 0:
        return cfg.weight, "MACD bullish crossover"
    if snap.macd < snap.macd_signal and (snap.macd_hist or 0) < 0:
        return -cfg.weight, "MACD bearish crossover"
    return 0, None


def _score_sma_cross(snap: IndicatorSnapshot, cfg) -> tuple[float, str | None]:
    if not cfg.enabled or None in (
        snap.sma_fast,
        snap.sma_slow,
        snap.sma_fast_prev,
        snap.sma_slow_prev,
    ):
        return 0, None
    crossed_up = snap.sma_fast_prev <= snap.sma_slow_prev and snap.sma_fast > snap.sma_slow
    crossed_dn = snap.sma_fast_prev >= snap.sma_slow_prev and snap.sma_fast < snap.sma_slow
    if crossed_up:
        return cfg.weight, f"Golden cross SMA{cfg.fast}/{cfg.slow}"
    if crossed_dn:
        return -cfg.weight, f"Death cross SMA{cfg.fast}/{cfg.slow}"
    # weak ongoing trend signal
    if snap.sma_fast > snap.sma_slow:
        return cfg.weight * 0.3, f"Trend up (SMA{cfg.fast}>SMA{cfg.slow})"
    return -cfg.weight * 0.3, f"Trend down (SMA{cfg.fast}<SMA{cfg.slow})"


def _score_bollinger(snap: IndicatorSnapshot, cfg) -> tuple[float, str | None]:
    if not cfg.enabled or None in (snap.bb_upper, snap.bb_lower):
        return 0, None
    if snap.close <= snap.bb_lower:
        return cfg.weight, "Price at/below lower Bollinger band"
    if snap.close >= snap.bb_upper:
        return -cfg.weight, "Price at/above upper Bollinger band"
    return 0, None


def _score_dividend_capture(
    symbol: str,
    cfg,
    upcoming: list[Dividend],
    today: date,
    last_price: float,
) -> tuple[float, str | None]:
    if not cfg.enabled or not upcoming:
        return 0, None
    upcoming_sorted = sorted(upcoming, key=lambda d: d.ex_date)
    next_div = next((d for d in upcoming_sorted if d.ex_date >= today), None)
    if next_div is None:
        return 0, None
    days = (next_div.ex_date - today).days
    if days < 0 or days > cfg.days_before_exdate:
        return 0, None
    if last_price <= 0:
        return 0, None
    annualized_yield_pct = next_div.amount * 4 / last_price * 100
    if annualized_yield_pct < cfg.min_yield_pct:
        return 0, None
    return (
        cfg.weight,
        f"ex-div in {days}d (${next_div.amount:.2f}, ~{annualized_yield_pct:.1f}% yield)",
    )


def evaluate(
    symbol: str,
    snap: IndicatorSnapshot,
    cfg: SignalsCfg,
    upcoming_dividends: list[Dividend],
    today: date,
    risk_atr_stop_mult: float,
    risk_atr_target_mult: float,
    capital_usd: float,
    max_position_pct: float,
    min_position_usd: float,
) -> Signal:
    score = 0.0
    reasons: list[str] = []

    for fn, rule in (
        (_score_rsi, cfg.rsi),
        (_score_macd, cfg.macd),
        (_score_sma_cross, cfg.sma_cross),
        (_score_bollinger, cfg.bollinger),
    ):
        s, reason = fn(snap, rule)
        score += s
        if reason:
            reasons.append(reason)

    s, reason = _score_dividend_capture(
        symbol, cfg.dividend_capture, upcoming_dividends, today, snap.close
    )
    score += s
    if reason:
        reasons.append(reason)

    if score >= cfg.buy_threshold:
        action = "BUY"
    elif score <= cfg.sell_threshold:
        action = "SELL"
    else:
        action = "HOLD"

    stop, target = None, None
    if snap.atr and snap.atr > 0:
        if action == "BUY":
            stop = snap.close - snap.atr * risk_atr_stop_mult
            target = snap.close + snap.atr * risk_atr_target_mult
        elif action == "SELL":
            stop = snap.close + snap.atr * risk_atr_stop_mult
            target = snap.close - snap.atr * risk_atr_target_mult

    suggested = capital_usd * (max_position_pct / 100)
    if suggested < min_position_usd:
        suggested = 0.0

    return Signal(
        symbol=symbol,
        action=action,
        score=round(score, 1),
        reasons=reasons,
        price=snap.close,
        stop=round(stop, 2) if stop else None,
        target=round(target, 2) if target else None,
        suggested_position_usd=round(suggested, 2),
    )
