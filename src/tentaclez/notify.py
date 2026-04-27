from __future__ import annotations

from telegram import Bot
from telegram.constants import ParseMode

from .strategy import LadderSignal


def fmt_signal(sig: LadderSignal) -> str:
    icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪️"}.get(sig.action, "•")
    head = f"{icon} <b>{sig.symbol}</b> {sig.action}"
    body: list[str] = []
    if sig.action == "BUY":
        body.append(f"buy ~${sig.suggested_amount_usd:.2f} @ ${sig.suggested_price:.2f}")
        if sig.target_price is not None:
            body.append(f"sell target ${sig.target_price:.2f}")
    elif sig.action == "SELL":
        body.append(f"sell {sig.suggested_qty:g} @ ${sig.suggested_price:.2f}")
        body.append(f"expected profit ${sig.expected_profit_usd:+.2f}")
    return head + "\n" + " | ".join(body) + "\n  • " + sig.reason


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID required")
        self._bot = Bot(token=token)
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        await self._bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    async def send_signal(self, sig: LadderSignal) -> None:
        await self.send(fmt_signal(sig))
