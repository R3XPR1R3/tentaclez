from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pandas_ta as ta


@dataclass(slots=True)
class IndicatorSnapshot:
    close: float
    rsi: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    sma_fast: float | None
    sma_slow: float | None
    sma_fast_prev: float | None
    sma_slow_prev: float | None
    bb_upper: float | None
    bb_lower: float | None
    bb_mid: float | None
    atr: float | None


def compute(
    df: pd.DataFrame,
    rsi_period: int,
    macd_fast: int,
    macd_slow: int,
    macd_signal_p: int,
    sma_fast: int,
    sma_slow: int,
    bb_period: int,
    bb_std: float,
    atr_period: int,
) -> IndicatorSnapshot:
    if df.empty or len(df) < max(sma_slow, macd_slow, bb_period, atr_period) + 5:
        return IndicatorSnapshot(
            close=float(df["close"].iloc[-1]) if not df.empty else 0.0,
            rsi=None,
            macd=None,
            macd_signal=None,
            macd_hist=None,
            sma_fast=None,
            sma_slow=None,
            sma_fast_prev=None,
            sma_slow_prev=None,
            bb_upper=None,
            bb_lower=None,
            bb_mid=None,
            atr=None,
        )

    close = df["close"]
    rsi = ta.rsi(close, length=rsi_period)
    macd_df = ta.macd(close, fast=macd_fast, slow=macd_slow, signal=macd_signal_p)
    sma_f = ta.sma(close, length=sma_fast)
    sma_s = ta.sma(close, length=sma_slow)
    bb = ta.bbands(close, length=bb_period, std=bb_std)
    atr = ta.atr(df["high"], df["low"], close, length=atr_period)

    macd_col = f"MACD_{macd_fast}_{macd_slow}_{macd_signal_p}"
    macds_col = f"MACDs_{macd_fast}_{macd_slow}_{macd_signal_p}"
    macdh_col = f"MACDh_{macd_fast}_{macd_slow}_{macd_signal_p}"
    bbu = f"BBU_{bb_period}_{bb_std}"
    bbl = f"BBL_{bb_period}_{bb_std}"
    bbm = f"BBM_{bb_period}_{bb_std}"

    def _last(series: pd.Series | None, idx: int = -1) -> float | None:
        if series is None or series.empty:
            return None
        try:
            v = series.iloc[idx]
        except IndexError:
            return None
        return float(v) if pd.notna(v) else None

    return IndicatorSnapshot(
        close=float(close.iloc[-1]),
        rsi=_last(rsi),
        macd=_last(macd_df[macd_col]) if macd_df is not None else None,
        macd_signal=_last(macd_df[macds_col]) if macd_df is not None else None,
        macd_hist=_last(macd_df[macdh_col]) if macd_df is not None else None,
        sma_fast=_last(sma_f),
        sma_slow=_last(sma_s),
        sma_fast_prev=_last(sma_f, -2),
        sma_slow_prev=_last(sma_s, -2),
        bb_upper=_last(bb[bbu]) if bb is not None else None,
        bb_lower=_last(bb[bbl]) if bb is not None else None,
        bb_mid=_last(bb[bbm]) if bb is not None else None,
        atr=_last(atr),
    )
