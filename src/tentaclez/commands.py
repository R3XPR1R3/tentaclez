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
from .snaptrade import SnapTradeClient


def _check_chat(update: Update, allowed_chat_id: str) -> bool:
    chat = update.effective_chat
    return chat is not None and str(chat.id) == str(allowed_chat_id)


def build_app(
    token: str,
    chat_id: str,
    cfg: Config,
    store: PortfolioStore,
    prices: PriceFetcher,
    snaptrade: SnapTradeClient | None = None,
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
        rh_block = (
            "\n<b>robinhood (read-only via SnapTrade)</b>\n"
            "/connect — link your Robinhood account (one-time browser tap)\n"
            "/rh — show live RH accounts + positions\n"
            "/disconnect — clear the stored link"
            if snaptrade is not None
            else ""
        )
        await update.message.reply_text(
            "<b>core</b>\n"
            "/status — portfolio + budgets + ladder anchors\n"
            "/buy SYMBOL AMOUNT_USD PRICE — record a manual buy ($-amount)\n"
            "/sell SYMBOL QTY PRICE — record a manual sell (FIFO, runs profit split)\n"
            "/cash — show total cash + per-ticker budget breakdown\n"
            "/budget [SYMBOL [AMOUNT|+AMOUNT|-AMOUNT]] — view or set a ticker's budget\n"
            "/transfer FROM TO AMOUNT — move cash between ticker budgets\n"
            "/rules — show per-ticker dip/profit thresholds\n"
            "/set SYMBOL PARAM VALUE — change dip|profit|freq for a ticker\n"
            "/history [N] — last N closed trades (default 10)\n"
            "/help — this message"
            + rh_block,
            parse_mode=ParseMode.HTML,
        )

    # ---- /status ----

    async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        budgets = await store.all_budgets()
        total_cash = sum(budgets.values())
        positions = await store.positions()
        lines = [f"💰 total cash: <b>${total_cash:.2f}</b>"]
        if budgets:
            lines.append("budgets:")
            for sym, amt in sorted(budgets.items()):
                lines.append(f"  {sym}: ${amt:.2f}")
        if not positions:
            lines.append("\nno open positions.")
        else:
            total_cost = 0.0
            total_market = 0.0
            pos_lines = ["\n<b>positions</b>:"]
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
                pos_lines.append(
                    f"  {p.symbol}: {p.qty:g} @ ${p.avg_price:.2f} → ${live:.2f} "
                    f"({'+' if pnl >= 0 else ''}{pnl:.2f} / {pnl_pct:+.1f}%)"
                )
            total_pnl = total_market - total_cost
            total_pct = (total_pnl / total_cost * 100) if total_cost else 0
            pos_lines.append(
                f"  market value ${total_market:.2f} (cost ${total_cost:.2f}, "
                f"P&L {'+' if total_pnl >= 0 else ''}{total_pnl:.2f} / {total_pct:+.1f}%)"
            )
            pos_lines.append(f"  <b>equity total</b>: ${total_market + total_cash:.2f}")
            lines.extend(pos_lines)
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
        new_budget = await store.adjust_budget(symbol, -amount_usd)
        await update.message.reply_text(
            f"recorded BUY {symbol} ${amount_usd:.2f} @ ${price:.2f} ({qty:.6f} sh)\n"
            f"sell target: ${target:.2f} (+{tcfg.profit_percent*100:.1f}%)\n"
            f"{symbol} budget: ${new_budget:.2f}"
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
        new_budget = await store.adjust_budget(symbol, proceeds)
        pnl = sum(t.pnl_usd for t in trades)
        msg = [
            f"recorded SELL {symbol} {qty:g} @ ${price:.2f}",
            f"proceeds: ${proceeds:.2f}  |  realized P&L: {'+' if pnl >= 0 else ''}${pnl:.2f}",
            f"{symbol} budget: ${new_budget:.2f}",
        ]
        if pnl > 0:
            split = split_profit(pnl, cfg.allocation)
            msg.append("")
            msg.append(split.fmt())
            if split.to_dividend > 0 and split.dividend_targets:
                first_target = split.dividend_targets[0]
                msg.append(
                    f"\nTo follow the {cfg.allocation.dividend_etf_pct:.0f}% rule: "
                    f"/transfer {symbol} {first_target} {split.to_dividend:.2f}"
                )
        await update.message.reply_text("\n".join(msg))

    # ---- /cash (read-only summary) + /budget (set/adjust) + /transfer ----

    async def cmd_cash(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        budgets = await store.all_budgets()
        if not budgets:
            await update.message.reply_text("no budgets set yet. use /budget SYMBOL AMOUNT")
            return
        total = sum(budgets.values())
        lines = [f"💰 total cash: <b>${total:.2f}</b>"]
        for sym, amt in sorted(budgets.items()):
            lines.append(f"  {sym}: ${amt:.2f}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        if len(ctx.args) == 0:
            return await cmd_cash(update, ctx)
        if len(ctx.args) == 1:
            symbol = ctx.args[0].upper()
            amt = await store.get_budget(symbol)
            await update.message.reply_text(f"{symbol}: ${amt:.2f}")
            return
        if len(ctx.args) != 2:
            await update.message.reply_text(
                "usage:\n  /budget SYMBOL AMOUNT — set\n"
                "  /budget SYMBOL +AMOUNT — add\n"
                "  /budget SYMBOL -AMOUNT — withdraw"
            )
            return
        symbol = ctx.args[0].upper()
        raw = ctx.args[1].strip()
        try:
            value = float(raw)
        except ValueError:
            await update.message.reply_text("amount must be a number")
            return
        if raw.startswith(("+", "-")):
            new_amt = await store.adjust_budget(symbol, value)
            await update.message.reply_text(
                f"{symbol} adjusted by ${value:+.2f} → budget ${new_amt:.2f}"
            )
        else:
            new_amt = await store.set_budget(symbol, value)
            await update.message.reply_text(f"{symbol} budget set to ${new_amt:.2f}")

    async def cmd_transfer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        if len(ctx.args) != 3:
            await update.message.reply_text("usage: /transfer FROM_SYMBOL TO_SYMBOL AMOUNT")
            return
        src, dst = ctx.args[0].upper(), ctx.args[1].upper()
        try:
            amount = float(ctx.args[2])
        except ValueError:
            await update.message.reply_text("amount must be a number")
            return
        if amount <= 0:
            await update.message.reply_text("amount must be positive")
            return
        src_budget = await store.get_budget(src)
        if src_budget < amount:
            await update.message.reply_text(
                f"{src} budget is ${src_budget:.2f}, can't transfer ${amount:.2f}"
            )
            return
        new_src = await store.adjust_budget(src, -amount)
        new_dst = await store.adjust_budget(dst, amount)
        await update.message.reply_text(
            f"transferred ${amount:.2f}: {src} → {dst}\n"
            f"  {src}: ${new_src:.2f}\n"
            f"  {dst}: ${new_dst:.2f}"
        )

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
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("transfer", cmd_transfer))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("history", cmd_history))

    # ---- SnapTrade-backed Robinhood read-only commands ----

    if snaptrade is not None:

        async def cmd_connect(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not await guard(update):
                return
            link = await store.get_brokerage_link()
            if link is None:
                user_id = SnapTradeClient.make_user_id()
                try:
                    user_secret = await snaptrade.register_user(user_id)
                except Exception as e:
                    logger.exception("snaptrade register failed")
                    await update.message.reply_text(f"snaptrade register failed: {e}")
                    return
                await store.save_brokerage_link(user_id, user_secret)
            else:
                user_id, user_secret = link
            try:
                url = await snaptrade.login_url(user_id, user_secret)
            except Exception as e:
                logger.exception("snaptrade login url failed")
                await update.message.reply_text(f"snaptrade login url failed: {e}")
                return
            await update.message.reply_text(
                "Tap this link → pick <b>Robinhood</b> → sign in. Comes back here when done.\n\n"
                f'<a href="{url}">connect Robinhood</a>\n\n'
                "(link is single-use and expires in 5 minutes.)",
                parse_mode=ParseMode.HTML,
            )

        async def cmd_rh(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not await guard(update):
                return
            link = await store.get_brokerage_link()
            if link is None:
                await update.message.reply_text(
                    "no brokerage linked yet. run /connect first."
                )
                return
            user_id, user_secret = link
            try:
                accounts = await snaptrade.list_accounts(user_id, user_secret)
            except Exception as e:
                logger.exception("snaptrade list_accounts failed")
                await update.message.reply_text(f"sync failed: {e}")
                return
            if not accounts:
                await update.message.reply_text(
                    "no brokerage accounts found. did the connect flow finish?"
                )
                return
            lines = ["<b>linked brokerage accounts</b>"]
            for acc in accounts:
                lines.append(
                    f"  {acc.institution} · {acc.name}  cash ${acc.cash:.2f}"
                )
                try:
                    positions = await snaptrade.get_holdings(
                        user_id, user_secret, acc.account_id
                    )
                except Exception as e:
                    logger.warning("holdings failed for {}: {}", acc.account_id, e)
                    lines.append(f"    (positions error: {e})")
                    continue
                if not positions:
                    lines.append("    (no positions)")
                    continue
                for p in positions:
                    avg = f"avg ${p.avg_buy_price:.2f}" if p.avg_buy_price else "avg —"
                    lines.append(
                        f"    {p.symbol}: {p.qty:g} @ ${p.price:.2f} "
                        f"= ${p.market_value:.2f}  ({avg})"
                    )
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

        async def cmd_disconnect(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if not await guard(update):
                return
            link = await store.get_brokerage_link()
            if link is None:
                await update.message.reply_text("nothing to disconnect.")
                return
            user_id, _ = link
            try:
                await snaptrade.delete_user(user_id)
            except Exception as e:
                logger.warning("snaptrade delete_user failed (will clear locally): {}", e)
            await store.clear_brokerage_link()
            await update.message.reply_text("disconnected. /connect to link again.")

        app.add_handler(CommandHandler("connect", cmd_connect))
        app.add_handler(CommandHandler("rh", cmd_rh))
        app.add_handler(CommandHandler("disconnect", cmd_disconnect))

    return app
