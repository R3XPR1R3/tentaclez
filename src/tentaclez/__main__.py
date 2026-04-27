from __future__ import annotations

import asyncio
import signal
import sys
from datetime import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from .commands import build_app
from .config import Env, load_config
from .engine import SignalRunner
from .market_hours import NY
from .notify import TelegramNotifier
from .portfolio import PortfolioStore
from .price import PriceFetcher


def _setup_logging(level: str) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level, enqueue=True)


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


async def _run() -> None:
    env = Env()
    cfg = load_config()
    _setup_logging(env.log_level)
    logger.info("tentaclez starting; watchlist: {}", cfg.watchlist())

    store = PortfolioStore(env.database_url)
    await store.init(ensure_symbols=cfg.watchlist())

    # Apply initial_budget_usd from config the first time we see a ticker.
    # This never overwrites an existing non-zero budget.
    current_budgets = await store.all_budgets()
    for tcfg in cfg.tickers:
        if tcfg.initial_budget_usd is None:
            continue
        sym = tcfg.symbol.upper()
        if current_budgets.get(sym, 0.0) == 0.0:
            await store.set_budget(sym, tcfg.initial_budget_usd)
            logger.info(
                "seeded {} budget with ${:.2f} from config", sym, tcfg.initial_budget_usd
            )

    prices = PriceFetcher(ttl_seconds=30.0)
    notifier = TelegramNotifier(env.telegram_bot_token, env.telegram_chat_id)
    runner = SignalRunner(cfg, prices, notifier, store)

    app = build_app(env.telegram_bot_token, env.telegram_chat_id, cfg, store, prices)

    scheduler = AsyncIOScheduler(timezone=NY)
    scheduler.add_job(
        runner.tick,
        IntervalTrigger(minutes=cfg.schedule.intraday_check_minutes),
        id="signal_tick",
        replace_existing=True,
    )
    pre = _parse_hhmm(cfg.schedule.pre_open_brief)
    post = _parse_hhmm(cfg.schedule.post_close_summary)
    scheduler.add_job(
        runner.pre_open_brief,
        CronTrigger(hour=pre.hour, minute=pre.minute, day_of_week="mon-fri"),
        id="pre_open",
        replace_existing=True,
    )
    scheduler.add_job(
        runner.post_close_summary,
        CronTrigger(hour=post.hour, minute=post.minute, day_of_week="mon-fri"),
        id="post_close",
        replace_existing=True,
    )

    stop_event = asyncio.Event()

    def _stop(*_: object) -> None:
        logger.info("shutdown requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    scheduler.start()
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    try:
        await notifier.send("🐙 tentaclez online. /help")
    except Exception as e:
        logger.warning("startup ping failed: {}", e)

    try:
        await stop_event.wait()
    finally:
        logger.info("stopping…")
        scheduler.shutdown(wait=False)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
