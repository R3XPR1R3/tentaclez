from __future__ import annotations

from datetime import date, timedelta

from loguru import logger

from .config import Config
from .data.finnhub import FinnhubClient
from .data.yfinance_src import fetch_history
from .indicators import compute
from .market_hours import is_market_open
from .notify import TelegramNotifier
from .portfolio import PortfolioStore
from .signal_engine import evaluate


class SignalRunner:
    """Polls watchlist, computes indicators, emits signals to Telegram."""

    def __init__(
        self,
        cfg: Config,
        finnhub: FinnhubClient,
        notifier: TelegramNotifier,
        store: PortfolioStore,
    ) -> None:
        self._cfg = cfg
        self._fh = finnhub
        self._notify = notifier
        self._store = store
        self._last_emitted: dict[str, str] = {}  # symbol -> last action emitted

    async def tick(self, force: bool = False) -> None:
        if not force and not is_market_open():
            logger.debug("market closed; skipping tick")
            return

        today = date.today()
        for symbol in self._cfg.watchlist.all_tickers():
            try:
                await self._evaluate_one(symbol, today)
            except Exception as e:
                logger.warning("eval {} failed: {}", symbol, e)

    async def _evaluate_one(self, symbol: str, today: date) -> None:
        df = await fetch_history(symbol, period="1y", interval="1d")
        if df.empty:
            logger.warning("no history for {}", symbol)
            return

        snap = compute(
            df,
            rsi_period=self._cfg.signals.rsi.period,
            macd_fast=self._cfg.signals.macd.fast,
            macd_slow=self._cfg.signals.macd.slow,
            macd_signal_p=self._cfg.signals.macd.signal,
            sma_fast=self._cfg.signals.sma_cross.fast,
            sma_slow=self._cfg.signals.sma_cross.slow,
            bb_period=self._cfg.signals.bollinger.period,
            bb_std=self._cfg.signals.bollinger.std,
            atr_period=self._cfg.risk.atr_period,
        )

        try:
            divs = await self._fh.dividends(
                symbol, today, today + timedelta(days=self._cfg.schedule.dividend_calendar_horizon_days)
            )
        except Exception as e:
            logger.debug("dividends fetch failed for {}: {}", symbol, e)
            divs = []

        sig = evaluate(
            symbol=symbol,
            snap=snap,
            cfg=self._cfg.signals,
            upcoming_dividends=divs,
            today=today,
            risk_atr_stop_mult=self._cfg.risk.stop_atr_mult,
            risk_atr_target_mult=self._cfg.risk.target_atr_mult,
            capital_usd=self._cfg.portfolio.total_capital_usd,
            max_position_pct=self._cfg.portfolio.max_position_pct,
            min_position_usd=self._cfg.portfolio.min_position_usd,
        )

        await self._store.log_signal(
            symbol=sig.symbol,
            action=sig.action,
            score=sig.score,
            price=sig.price,
            reasons=sig.reasons,
        )

        # only push to Telegram when action is actionable AND state changed
        if sig.action in ("BUY", "SELL") and self._last_emitted.get(symbol) != sig.action:
            bucket = self._cfg.watchlist.bucket_of(symbol)
            await self._notify.send_signal(sig, bucket)
            self._last_emitted[symbol] = sig.action
        elif sig.action == "HOLD":
            self._last_emitted.pop(symbol, None)

    async def pre_open_brief(self) -> None:
        msg = (
            "☕️ <b>pre-open brief</b>\n"
            f"watchlist: {len(self._cfg.watchlist.all_tickers())} tickers\n"
            "tracking signals during today's session."
        )
        await self._notify.send(msg)

    async def post_close_summary(self) -> None:
        positions = await self._store.positions()
        lines = ["🌙 <b>post-close summary</b>"]
        if not positions:
            lines.append("no open positions.")
        else:
            total_cost = 0.0
            total_market = 0.0
            for p in positions:
                try:
                    q = await self._fh.quote(p.symbol)
                    live = q.price
                except Exception:
                    live = p.avg_price
                mv = p.qty * live
                total_cost += p.cost_basis
                total_market += mv
                lines.append(f"{p.symbol}: {p.qty:g} @ ${p.avg_price:.2f} → ${live:.2f}")
            pnl = total_market - total_cost
            lines.append(
                f"\ntotal market: ${total_market:.2f}, "
                f"P&L {'+' if pnl >= 0 else ''}${pnl:.2f}"
            )
        await self._notify.send("\n".join(lines))
