# ТЗ — In-chat setup wizard + Raspberry Pi installer

> Передача следующему агенту (Codex). Стартовая точка: коммит `73f548d` на ветке `claude/trading-signal-bot-0nJd7`.

## Цель

Все секреты и привязки убрать из `.env` в Telegram. В `.env` остаются только `TELEGRAM_BOT_TOKEN` и `TELEGRAM_OWNER_USERNAME=barckhat`. Остальное настраивается командами в чате. Если ключа SnapTrade нет — бот **сам спрашивает** в чате через `ConversationHandler`. Плюс `scripts/install.sh` для Raspberry Pi: один прогон, ставит Docker, пишет `.env`, поднимает контейнер.

## Состояние репо

- Ветка: `claude/trading-signal-bot-0nJd7`
- Зелёный коммит-старт: `73f548d` (SnapTrade с ключами в env)
- Локально чисто, незакоммиченных правок нет.

## Что сделать

### 1. БД: `Settings` k/v таблица

В `src/tentaclez/portfolio.py`:

```python
class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(2048))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
```

Методы на `PortfolioStore`:
- `get_setting(key) -> str | None`
- `set_setting(key, value)`
- `delete_setting(key)`

Ключи (объявить константами в `config.py`):
- `SETTING_CHAT_ID = "telegram_chat_id"`
- `SETTING_SNAPTRADE_CLIENT_ID = "snaptrade_client_id"`
- `SETTING_SNAPTRADE_CONSUMER_KEY = "snaptrade_consumer_key"`

### 2. `.env` ужать до 2 полей

В `src/tentaclez/config.py` класс `Env`:
- Убрать `telegram_chat_id`, `snaptrade_client_id`, `snaptrade_consumer_key`.
- Добавить `telegram_owner_username` (без `@`, lowercase для сравнения).
- Метод `normalized_owner()` — стрипает `@` и lowercase.

`.env.example`:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_USERNAME=barckhat
DATABASE_URL=sqlite+aiosqlite:////app/data/tentaclez.db
LOG_LEVEL=INFO
TZ=America/New_York
```

### 3. Notifier с динамическим chat_id

В `src/tentaclez/notify.py`:

```python
ChatIdGetter = Callable[[], Awaitable[str | None]]

class TelegramNotifier:
    def __init__(self, token: str, chat_id_getter: ChatIdGetter):
        self._bot = Bot(token=token)
        self._get_chat_id = chat_id_getter

    async def send(self, text: str) -> bool:
        chat_id = await self._get_chat_id()
        if chat_id is None:
            logger.debug("no chat bound; skipping: {}", text[:80])
            return False
        await self._bot.send_message(chat_id=chat_id, text=text, ...)
        return True
```

Вызовы `notifier.send_signal()` и `notifier.send()` должны no-op'ить если chat не привязан, без падения.

### 4. Auto-bind по username

В `commands.py` хелпер вместо `_check_chat`:

```python
async def authorized(update) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        return False
    stored = await store.get_setting(SETTING_CHAT_ID)
    if stored is None:
        # not bound yet — only owner-by-username can claim
        if user is None:
            return False
        if (user.username or "").lower() != owner_username.lower():
            logger.warning("unauthorized claim attempt by @{}", user.username)
            return False
        await store.set_setting(SETTING_CHAT_ID, str(chat.id))
        logger.info("chat bound: @{} → chat_id {}", user.username, chat.id)
        return True
    return str(chat.id) == stored
```

Заменить везде старую `guard()` / `_check_chat` на `authorized()`.

### 5. Lazy SnapTrade client

Закрытие в `commands.py`:

```python
async def get_snaptrade() -> SnapTradeClient | None:
    cid = await store.get_setting(SETTING_SNAPTRADE_CLIENT_ID)
    ck = await store.get_setting(SETTING_SNAPTRADE_CONSUMER_KEY)
    if not cid or not ck:
        return None
    try:
        return SnapTradeClient(cid, ck)
    except Exception as e:
        logger.warning("snaptrade init failed: {}", e)
        return None
```

`/connect`, `/rh`, `/disconnect` дёргают `get_snaptrade()` внутри. Если `None` — **автоматически** запускают `/snaptrade` диалог.

### 6. ConversationHandler `/snaptrade`

Двухшаговый wizard:

```python
SNAP_CLIENT_ID, SNAP_CONSUMER_KEY = range(2)

snap_conv = ConversationHandler(
    entry_points=[CommandHandler("snaptrade", snap_start)],
    states={
        SNAP_CLIENT_ID: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, snap_client_id)
        ],
        SNAP_CONSUMER_KEY: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, snap_consumer_key)
        ],
    },
    fallbacks=[CommandHandler("cancel", snap_cancel)],
)
```

Шаг 1 (`snap_start`): "Send Client ID. Looks like `PERS-XXXXX`. /cancel to abort." → возврат `SNAP_CLIENT_ID`.

Шаг 2 (`snap_client_id`): сохранить в `ctx.user_data["snap_cid"]`, попросить Consumer Key. **Обязательно**: "⚠️ Это секрет. Удали сообщение вручную (tap-and-hold → Delete) — Telegram не даёт боту удалять сообщения юзера в личках." → возврат `SNAP_CONSUMER_KEY`.

Шаг 3 (`snap_consumer_key`): валидация через `SnapTradeClient(cid, ck)`; при успехе `store.set_setting()` обоих, ответить "✅ saved. Удали свои 2 последних сообщения. Дальше /connect для привязки RH". → `ConversationHandler.END`.

`/forget_snaptrade`: удалить оба setting'а; если есть `BrokerageLink` — позвать `client.delete_user(user_id)` и `clear_brokerage_link()`.

### 7. Проактивный nudge

`/start` после auto-bind должна проверять что не сконфигурено и подталкивать:

```python
if await get_snaptrade() is None:
    msg += "\n⚠️ Robinhood read-only sync не настроен. /snaptrade для конфигурации."
```

`/connect` если SnapTrade keys пустые — **сразу запускать** `snap_start` (entrypoint conversation handler'а), а не отвечать «run /snaptrade first».

### 8. `__main__.py`

- Прочитать `env.telegram_owner_username`. Если пусто — упасть с понятной ошибкой.
- `chat_id_getter = partial(store.get_setting, SETTING_CHAT_ID)` — замыкание/частичная.
- `notifier = TelegramNotifier(token, chat_id_getter)`.
- Убрать построение `SnapTradeClient` на старте — теперь lazy.
- `app = build_app(token, owner_username, cfg, store, prices)` — без chat_id и без snaptrade.
- Стартовый `notifier.send("🐙 online")` оставить, он сам no-op'нет если chat не привязан.

### 9. `scripts/install.sh` для Raspberry Pi

Bash, идемпотентный:

1. `set -euo pipefail`.
2. Detect OS: `/etc/os-release` ID должен быть `debian`/`ubuntu`/`raspbian`.
3. Если нет `docker` — поставить через `https://get.docker.com` convenience-script + `usermod -aG docker $USER`.
4. Если нет docker compose plugin — `apt-get install -y docker-compose-plugin`.
5. Если нет `.env` — спросить интерактивно `TELEGRAM_BOT_TOKEN` и `TELEGRAM_OWNER_USERNAME` (default = `barckhat`), записать в `.env`. `chmod 600`.
6. Если нет `config.yaml` — `cp config.yaml.example config.yaml`.
7. `mkdir -p data`.
8. `docker compose up --build -d`.
9. Тейл логов `docker compose logs -f bot --tail 50` пока не увидит "tentaclez online" или Ctrl+C.
10. Финальный echo с инструкцией: «Open Telegram, message your bot as @${OWNER}, send /start. Then /snaptrade to add SnapTrade keys.»

Положить в `scripts/install.sh`, `chmod +x`.

### 10. README + `.env.example`

- Удалить раздел про ручную правку `.env` для SnapTrade.
- Добавить «Quick start on Raspberry Pi» — один вызов `bash scripts/install.sh`.
- Описать новый flow: `/start` → бот биндится → `/snaptrade` → wizard → `/connect` → готово.

### 11. Тесты

В `tests/` юнит-тесты для `authorized()`. Замокать `store.get_setting`/`set_setting`. Проверить:
- Пустой `chat_id` + правильный username → биндит и пускает.
- Пустой `chat_id` + неправильный username → отказ.
- Забинденный chat_id + совпадающий chat → пускает.
- Забинденный chat_id + другой chat → отказ.

### 12. Финал

`pytest` зелёный, `python -c "import ast; …"` без ошибок. Один коммит «in-chat setup wizard + RPi installer». `git push origin claude/trading-signal-bot-0nJd7`.

---

## Подводные камни

- **Telegram API не даёт боту удалять сообщения юзера в private chat**. Не вызывать `update.message.delete()` на сообщения юзера — упадёт. Просить руками.
- **SnapTrade SDK импорт guard**: `try: from snaptrade_client import SnapTrade except: SnapTrade = None`. Проверка на `None` — внутри ленивого getter'а, не в import time.
- **Первый старт на чистой БД**: scheduled jobs (pre-open brief, post-close summary) могут отработать **до** того как юзер написал боту. Notifier должен тихо no-op'нуть. См. п.3.
- `python-telegram-bot` `ConversationHandler` регистрировать **до** обычных `CommandHandler`'ов или использовать `group=` если конфликтует с глобальной `/cancel`.
- `OWNER_USERNAME` сравнивается case-insensitive, без `@`. Если юзер в env написал `@Barckhat` — нормализовать через `normalized_owner()`.
- SDK SnapTrade возвращает то Pydantic-объекты, то dict — текущая обвязка в `snaptrade.py` это уже учитывает через `getattr` + `.get()` fallback.
