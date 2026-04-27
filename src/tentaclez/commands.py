from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .allocator import split_profit
from .config import Config, TickerCfg
from .portfolio import PortfolioStore
from .price import PriceFetcher


def _check_chat(update: Update, allowed_chat_id: str) -> bool:
    chat = update.effective_chat
    return chat is not None and str(chat.id) == str(allowed_chat_id)


def build_app(
    token: str,
    chat_id: str,
    cfg: Config,
    store: PortfolioStore,
    prices: PriceFetcher,
) -> Application:
    app = Application.builder().token(token).build()

    async def guard(update: Update) -> bool:
        if not _check_chat(update, chat_id):
            logger.warning("ignoring message from chat {}", update.effective_chat)
            return False
        return True

    # ---- /start /help ----

    async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        await update.message.reply_text(
            "🐙 tentaclez — ladder swing bot.\n"
            "type /help to see commands."
        )

    async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        await update.message.reply_text(
            "<b>commands</b>\n"
            "/status — portfolio + cash + ladder anchors\n"
            "/buy SYMBOL AMOUNT_USD PRICE — record a manual buy ($-amount)\n"
            "/sell SYMBOL QTY PRICE — record a manual sell (FIFO, runs profit split)\n"
            "/cash AMOUNT — set free cash (use a leading + or - to adjust)\n"
            "/rules — show per-ticker dip/profit thresholds\n"
            "/set SYMBOL PARAM VALUE — change dip|profit|freq for a ticker\n"
            "/history [N] — last N closed trades (default 10)\n"
            "/help — this message",
            parse_mode=ParseMode.HTML,
        )

    # ---- /status ----

    async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        cash = await store.get_cash()
        positions = await store.positions()
        lines = [f"💰 free cash: <b>${cash:.2f}</b>"]
        if not positions:
            lines.append("no open positions.")
        else:
            total_cost = 0.0
            total_market = 0.0
            for p in positions:
                try:
                    snap = await prices.get(p.symbol)
                    live = snap.price
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
                f"\n<b>positions value</b>: ${total_market:.2f} (cost ${total_cost:.2f}, "
                f"P&L {'+' if total_pnl >= 0 else ''}{total_pnl:.2f} / {total_pct:+.1f}%)"
            )
            lines.append(f"<b>total equity</b>: ${total_market + cash:.2f}")
        # next ladder triggers
        if cfg.tickers:
            lines.append("\n<b>ladder anchors</b>:")
            for t in cfg.tickers:
                last = await store.last_buy_price(t.symbol)
                if last is None:
                    lines.append(f"  {t.symbol}: no anchor yet")
                    continue
                dip = last * (1 - t.dip_percent)
                lines.append(
                    f"  {t.symbol}: last ${last:.2f} → next buy ≤ ${dip:.2f}"
                )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # ---- /buy ----

    async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        if len(ctx.args) != 3:
            await update.message.reply_text("usage: /buy SYMBOL AMOUNT_USD PRICE")
            return
        try:
            symbol = ctx.args[0].upper()
            amount_usd = float(ctx.args[1])
            price = float(ctx.args[2])
        except ValueError:
            await update.message.reply_text("amount and price must be numbers")
            return
        if amount_usd <= 0 or price <= 0:
            await update.message.reply_text("amount and price must be positive")
            return
        tcfg = cfg.ticker(symbol)
        if tcfg is None:
            await update.message.reply_text(
                f"{symbol} is not in your watchlist. add it to config.yaml first."
            )
            return
        qty = amount_usd / price
        target = price * (1 + tcfg.profit_percent)
        await store.add_lot(
            symbol=symbol,
            qty=qty,
            entry_price=price,
            target_price=target,
            at=datetime.now(tz=timezone.utc),
        )
        new_cash = await store.adjust_cash(-amount_usd)
        await update.message.reply_text(
            f"recorded BUY {symbol} ${amount_usd:.2f} @ ${price:.2f} ({qty:.6f} sh)\n"
            f"sell target: ${target:.2f} (+{tcfg.profit_percent*100:.1f}%)\n"
            f"free cash: ${new_cash:.2f}"
        )

    # ---- /sell ----

    async def cmd_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        if len(ctx.args) != 3:
            await update.message.reply_text("usage: /sell SYMBOL QTY PRICE")
            return
        try:
            symbol = ctx.args[0].upper()
            qty = float(ctx.args[1])
            price = float(ctx.args[2])
        except ValueError:
            await update.message.reply_text("qty and price must be numbers")
            return
        if qty <= 0 or price <= 0:
            await update.message.reply_text("qty and price must be positive")
            return
        try:
            trades = await store.close_position(symbol, qty, price)
        except ValueError as e:
            await update.message.reply_text(f"error: {e}")
            return
        proceeds = qty * price
        new_cash = await store.adjust_cash(proceeds)
        pnl = sum(t.pnl_usd for t in trades)
        msg = [
            f"recorded SELL {symbol} {qty:g} @ ${price:.2f}",
            f"proceeds: ${proceeds:.2f}  |  realized P&L: {'+' if pnl >= 0 else ''}${pnl:.2f}",
            f"free cash: ${new_cash:.2f}",
        ]
        if pnl > 0:
            split = split_profit(pnl, cfg.allocation)
            msg.append("")
            msg.append(split.fmt())
        await update.message.reply_text("\n".join(msg))

    # ---- /cash ----

    async def cmd_cash(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        if len(ctx.args) == 0:
            cash = await store.get_cash()
            await update.message.reply_text(f"free cash: ${cash:.2f}")
            return
        if len(ctx.args) != 1:
            await update.message.reply_text("usage: /cash AMOUNT  (or +AMOUNT / -AMOUNT to adjust)")
            return
        raw = ctx.args[0].strip()
        try:
            value = float(raw)
        except ValueError:
            await update.message.reply_text("amount must be a number")
            return
        if raw.startswith(("+", "-")):
            new_cash = await store.adjust_cash(value)
            await update.message.reply_text(f"adjusted by ${value:+.2f} → free cash ${new_cash:.2f}")
        else:
            new_cash = await store.set_cash(value)
            await update.message.reply_text(f"free cash set to ${new_cash:.2f}")

    # ---- /rules ----

    async def cmd_rules(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        if not cfg.tickers:
            await update.message.reply_text("no tickers configured.")
            return
        lines = ["<b>ladder rules</b>"]
        for t in cfg.tickers:
            lines.append(
                f"{t.symbol}: dip {t.dip_percent*100:.1f}% | "
                f"profit {t.profit_percent*100:.1f}% | "
                f"check ~{t.check_frequency_minutes}m"
            )
        lines.append(
            f"\nsizing: {cfg.sizing.min_trade_pct*100:.0f}–{cfg.sizing.max_trade_pct*100:.0f}% of free cash"
        )
        lines.append(
            f"profit split: {cfg.allocation.reinvest_trade_pct:.0f}% trade pool / "
            f"{cfg.allocation.dividend_etf_pct:.0f}% dividend "
            f"({', '.join(cfg.allocation.dividend_etf_symbols)})"
        )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # ---- /set ----

    async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        if len(ctx.args) != 3:
            await update.message.reply_text(
                "usage: /set SYMBOL PARAM VALUE\nPARAM: dip | profit | freq\n"
                "values: dip/profit as fraction (0.02 = 2%) OR percent suffix (2%)"
            )
            return
        symbol, param, raw = ctx.args[0].upper(), ctx.args[1].lower(), ctx.args[2]
        tcfg = cfg.ticker(symbol)
        if tcfg is None:
            await update.message.reply_text(
                f"{symbol} not in watchlist. add it to config.yaml first."
            )
            return
        try:
            value = float(raw.rstrip("%"))
            if raw.endswith("%"):
                value /= 100
        except ValueError:
            await update.message.reply_text("value must be a number")
            return
        if param == "dip":
            cfg.upsert_ticker(TickerCfg(**(tcfg.model_dump() | {"dip_percent": value})))
        elif param == "profit":
            cfg.upsert_ticker(TickerCfg(**(tcfg.model_dump() | {"profit_percent": value})))
        elif param == "freq":
            cfg.upsert_ticker(
                TickerCfg(**(tcfg.model_dump() | {"check_frequency_minutes": int(value)}))
            )
        else:
            await update.message.reply_text(f"unknown param '{param}'. use dip|profit|freq")
            return
        await update.message.reply_text(
            f"{symbol}.{param} set to {value} (in-memory only — persist by editing config.yaml)"
        )

    # ---- /history ----

    async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        n = 10
        if ctx.args:
            try:
                n = max(1, min(100, int(ctx.args[0])))
            except ValueError:
                pass
        trades = await store.recent_trades(limit=n)
        if not trades:
            await update.message.reply_text("no closed trades yet.")
            return
        lines = [f"<b>last {len(trades)} trades</b>"]
        total_pnl = 0.0
        total_cost = 0.0
        for t in trades:
            cost = t.qty * t.entry_price
            roi = (t.pnl_usd / cost * 100) if cost else 0
            total_pnl += t.pnl_usd
            total_cost += cost
            lines.append(
                f"{t.exit_at.date().isoformat()} {t.symbol} {t.qty:g} "
                f"${t.entry_price:.2f}→${t.exit_price:.2f} "
                f"P&L {'+' if t.pnl_usd >= 0 else ''}${t.pnl_usd:.2f} ({roi:+.1f}%)"
            )
        avg_roi = (total_pnl / total_cost * 100) if total_cost else 0
        lines.append(
            f"\ntotals: P&L {'+' if total_pnl >= 0 else ''}${total_pnl:.2f} | "
            f"avg ROI {avg_roi:+.2f}%"
        )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler("cash", cmd_cash))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("history", cmd_history))
    return app
