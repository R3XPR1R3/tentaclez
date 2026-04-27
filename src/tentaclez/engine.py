from __future__ import annotations

from loguru import logger

from .config import Config
from .market_hours import is_market_open
from .notify import TelegramNotifier
from .portfolio import PortfolioStore
from .price import PriceFetcher
from .strategy import StrategyParams, TickerState, generate_signal


class SignalRunner:
    """Polls watchlist tickers, runs the ladder strategy, emits Telegram alerts."""

    def __init__(
        self,
        cfg: Config,
        prices: PriceFetcher,
        notifier: TelegramNotifier,
        store: PortfolioStore,
    ) -> None:
        self._cfg = cfg
        self._prices = prices
        self._notify = notifier
        self._store = store
        # debounce: don't re-send the same actionable state every 10 minutes
        self._last_emitted: dict[str, str] = {}

    async def tick(self, force: bool = False) -> None:
        if not force and not is_market_open():
            logger.debug("market closed; skipping tick")
            return

        free_cash = await self._store.get_cash()
        for ticker_cfg in self._cfg.tickers:
            try:
                await self._evaluate_one(ticker_cfg.symbol, free_cash)
            except Exception as e:
                logger.warning("eval {} failed: {}", ticker_cfg.symbol, e)

    async def _evaluate_one(self, symbol: str, free_cash: float) -> None:
        ticker_cfg = self._cfg.ticker(symbol)
        if ticker_cfg is None:
            return
        snap = await self._prices.get(symbol)
        last_buy = await self._store.last_buy_price(symbol)
        open_lots = await self._store.open_lots_for_strategy(symbol)
        state = TickerState(
            symbol=symbol.upper(),
            current_price=snap.price,
            last_buy_price=last_buy,
            open_lots=open_lots,
            free_cash=free_cash,
        )
        params = StrategyParams(
            dip_percent=ticker_cfg.dip_percent,
            profit_percent=ticker_cfg.profit_percent,
            min_trade_pct=self._cfg.sizing.min_trade_pct,
            max_trade_pct=self._cfg.sizing.max_trade_pct,
        )
        sig = generate_signal(state, params)

        await self._store.log_signal(
            symbol=sig.symbol,
            action=sig.action,
            price=snap.price,
            reason=sig.reason,
        )

        # Push only on actionable transitions to avoid alert spam.
        key = f"{sig.symbol}:{sig.action}:{sig.lot_id or 0}:{sig.target_price or 0:.2f}"
        if sig.action in ("BUY", "SELL") and self._last_emitted.get(sig.symbol) != key:
            await self._notify.send_signal(sig)
            self._last_emitted[sig.symbol] = key
        elif sig.action == "HOLD":
            self._last_emitted.pop(sig.symbol, None)

    async def pre_open_brief(self) -> None:
        cash = await self._store.get_cash()
        msg = [
            "☕️ <b>pre-open brief</b>",
            f"watchlist: {', '.join(self._cfg.watchlist()) or '—'}",
            f"free cash: ${cash:.2f}",
        ]
        for tcfg in self._cfg.tickers:
            last = await self._store.last_buy_price(tcfg.symbol)
            if last is None:
                msg.append(
                    f"  {tcfg.symbol}: no anchor yet — record a /buy to start the ladder"
                )
                continue
            dip = last * (1 - tcfg.dip_percent)
            msg.append(
                f"  {tcfg.symbol}: last ${last:.2f} → next buy ≤ ${dip:.2f} "
                f"(-{tcfg.dip_percent*100:.1f}%)"
            )
        await self._notify.send("\n".join(msg))

    async def post_close_summary(self) -> None:
        positions = await self._store.positions()
        cash = await self._store.get_cash()
        lines = ["🌙 <b>post-close summary</b>", f"free cash: ${cash:.2f}"]
        if not positions:
            lines.append("no open positions.")
        else:
            total_cost = 0.0
            total_market = 0.0
            for p in positions:
                try:
                    snap = await self._prices.get(p.symbol)
                    live = snap.price
                except Exception:
                    live = p.avg_price
                mv = p.qty * live
                total_cost += p.cost_basis
                total_market += mv
                lines.append(f"{p.symbol}: {p.qty:g} @ ${p.avg_price:.2f} → ${live:.2f}")
            pnl = total_market - total_cost
            lines.append(
                f"\nmarket value ${total_market:.2f} | cost ${total_cost:.2f} | "
                f"unreal P&L {'+' if pnl >= 0 else ''}${pnl:.2f}"
            )
        await self._notify.send("\n".join(lines))
