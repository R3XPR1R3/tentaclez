from __future__ import annotations

from telegram import Bot
from telegram.constants import ParseMode

from .signal_engine import Signal


def fmt_signal(sig: Signal, bucket: str) -> str:
    icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪️"}.get(sig.action, "•")
    head = f"{icon} <b>{sig.symbol}</b> [{bucket}]  {sig.action}  score {sig.score:+.0f}"
    body = [f"price: ${sig.price:.2f}"]
    if sig.stop is not None:
        body.append(f"stop: ${sig.stop:.2f}")
    if sig.target is not None:
        body.append(f"target: ${sig.target:.2f}")
    if sig.suggested_position_usd > 0 and sig.action == "BUY":
        body.append(f"size: ~${sig.suggested_position_usd:.0f}")
    reasons = "\n".join(f"  • {r}" for r in sig.reasons) or "  (no triggers)"
    return f"{head}\n{' | '.join(body)}\n{reasons}"


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

    async def send_signal(self, sig: Signal, bucket: str) -> None:
        await self.send(fmt_signal(sig, bucket))
