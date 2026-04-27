from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .allocator import split_profit
from .config import Config
from .data.finnhub import FinnhubClient
from .portfolio import PortfolioStore


def _check_chat(update: Update, allowed_chat_id: str) -> bool:
    chat = update.effective_chat
    return chat is not None and str(chat.id) == str(allowed_chat_id)


def build_app(
    token: str,
    chat_id: str,
    cfg: Config,
    store: PortfolioStore,
    finnhub: FinnhubClient,
) -> Application:
    app = Application.builder().token(token).build()

    async def guard(update: Update) -> bool:
        if not _check_chat(update, chat_id):
            logger.warning("ignoring message from unauthorized chat {}", update.effective_chat)
            return False
        return True

    async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        await update.message.reply_text(
            "tentaclez online.\n"
            "Commands: /status /watchlist /portfolio /add /sell /closed /divcal /help"
        )

    async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        await update.message.reply_text(
            "/status — bot state\n"
            "/watchlist — configured tickers\n"
            "/portfolio — open positions + live P&L\n"
            "/add SYMBOL QTY PRICE — record a manual buy\n"
            "/sell SYMBOL QTY PRICE — record a manual sell (FIFO)\n"
            "/closed SYMBOL QTY PRICE — alias of /sell, also runs profit split\n"
            "/divcal — upcoming ex-dividend dates for watchlist"
        )

    async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        positions = await store.positions()
        await update.message.reply_text(
            f"running.\n"
            f"watchlist: {len(cfg.watchlist.all_tickers())} tickers\n"
            f"open positions: {len(positions)}\n"
            f"capital cap: ${cfg.portfolio.total_capital_usd:.0f}"
        )

    async def cmd_watchlist(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        wl = cfg.watchlist
        text = (
            f"<b>core</b>: {', '.join(wl.core) or '—'}\n"
            f"<b>growth</b>: {', '.join(wl.growth) or '—'}\n"
            f"<b>dividend</b>: {', '.join(wl.dividend) or '—'}\n"
            f"<b>sector</b>: {', '.join(wl.sector) or '—'}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_portfolio(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        positions = await store.positions()
        if not positions:
            await update.message.reply_text("no open positions. /add SYMBOL QTY PRICE to record one.")
            return
        lines = ["<b>portfolio</b>"]
        total_cost = 0.0
        total_market = 0.0
        for p in positions:
            try:
                q = await finnhub.quote(p.symbol)
                live = q.price
            except Exception as e:
                logger.warning("quote failed for {}: {}", p.symbol, e)
                live = p.avg_price
            mv = p.qty * live
            pnl = mv - p.cost_basis
            pnl_pct = (pnl / p.cost_basis * 100) if p.cost_basis else 0
            total_cost += p.cost_basis
            total_market += mv
            lines.append(
                f"{p.symbol}: {p.qty:g} @ ${p.avg_price:.2f} → ${live:.2f} "
                f"({'+' if pnl >= 0 else ''}{pnl:.2f} / {pnl_pct:+.1f}%)"
            )
        total_pnl = total_market - total_cost
        total_pct = (total_pnl / total_cost * 100) if total_cost else 0
        lines.append(
            f"\n<b>total</b>: ${total_market:.2f} (cost ${total_cost:.2f}, "
            f"P&L {'+' if total_pnl >= 0 else ''}{total_pnl:.2f} / {total_pct:+.1f}%)"
        )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _parse_trade_args(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> tuple[str, float, float] | None:
        if len(ctx.args) != 3:
            await update.message.reply_text("usage: /add SYMBOL QTY PRICE")
            return None
        try:
            return ctx.args[0].upper(), float(ctx.args[1]), float(ctx.args[2])
        except ValueError:
            await update.message.reply_text("qty and price must be numbers")
            return None

    async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        parsed = await _parse_trade_args(update, ctx)
        if not parsed:
            return
        symbol, qty, price = parsed
        await store.add_lot(symbol, qty, price, at=datetime.now(tz=timezone.utc))
        await update.message.reply_text(
            f"recorded BUY {symbol} {qty:g} @ ${price:.2f} (cost ${qty*price:.2f})"
        )

    async def cmd_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        parsed = await _parse_trade_args(update, ctx)
        if not parsed:
            return
        symbol, qty, price = parsed
        try:
            trades = await store.close_position(symbol, qty, price)
        except ValueError as e:
            await update.message.reply_text(f"error: {e}")
            return
        pnl = sum(t.pnl_usd for t in trades)
        msg = [f"recorded SELL {symbol} {qty:g} @ ${price:.2f}"]
        msg.append(f"realized P&L: {'+' if pnl >= 0 else ''}${pnl:.2f}")
        if pnl > 0:
            split = split_profit(pnl, cfg.allocation)
            msg.append("")
            msg.append(split.fmt())
        await update.message.reply_text("\n".join(msg))

    async def cmd_divcal(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        from datetime import date, timedelta

        today = date.today()
        horizon = today + timedelta(days=cfg.schedule.dividend_calendar_horizon_days)
        rows: list[tuple[str, str, float]] = []
        for symbol in cfg.watchlist.all_tickers():
            try:
                divs = await finnhub.dividends(symbol, today, horizon)
            except Exception as e:
                logger.warning("dividend lookup failed for {}: {}", symbol, e)
                continue
            for d in divs:
                if d.ex_date >= today:
                    rows.append((d.ex_date.isoformat(), symbol, d.amount))
        if not rows:
            await update.message.reply_text("no upcoming ex-dividend dates in watchlist.")
            return
        rows.sort()
        lines = [f"<b>ex-div next {cfg.schedule.dividend_calendar_horizon_days}d</b>"]
        for ex, sym, amt in rows:
            lines.append(f"{ex}  {sym}  ${amt:.2f}/share")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler("closed", cmd_sell))  # alias
    app.add_handler(CommandHandler("divcal", cmd_divcal))
    return app
