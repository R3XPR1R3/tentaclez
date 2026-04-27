# tentaclez

Сигнальный бот для торговли акциями и ETF на Robinhood — **исполнение только руками**.
Бот сам ничего не покупает: считает индикаторы, шлёт алерты в Telegram, ведёт paper-портфель, считает дивиденды и предлагает как делить прибыль. Когда созреешь до автотрейдинга — слой исполнения подключим через Alpaca, без переписывания.

## Зачем это так

У Robinhood **нет официального API для акций/ETF** в 2026 — только крипта.
Все «робингуд-боты» в гитхабе лезут в приватный API, что нарушает ToS и **может стоить аккаунта**. Поэтому здесь:

- **данные** идём за: Finnhub (60 req/min free) + yfinance (бэкап/история),
- **исполнение** делаешь руками в приложении Robinhood,
- **позиции** записываешь в бот командами `/add` / `/sell`.

## Что уже умеет (v0.1)

- Считает по watchlist индикаторы: **RSI, MACD, SMA-кросс 50/200, Bollinger, ATR**.
- Композитный скор `[-100..+100]` → BUY/SELL/HOLD по порогам в `config.yaml`.
- ATR-стоп и тейк, разумный размер позиции в долларах (учитывает fractional shares).
- Учитывает **расписание NYSE** (праздники, half-days) — вне сессии тиков нет.
- **Дивидендный модуль**: добавляет к скору если ex-date близко, отдельная команда `/divcal`.
- **Paper-портфель** в SQLite (FIFO-матчинг, реализованная P&L).
- **Профит-сплит**: при `/sell` с прибылью бот предлагает разнести: % в core ETF, % в дивидендник, % в кэш.
- Утренний бриф и вечернее ресюме в Telegram.

## Чего пока нет (специально)

- Авто-исполнение, реальные ордера. Будет в v0.2 через Alpaca paper, потом live.
- Опционы, маржа, шорты. Не нужны для accumulation-стратегии.
- ML/LLM. Сначала прозрачные правила, потом — слой объяснения через Claude API.
- Бэктест. Идёт следующим в roadmap.

## Запуск за 5 минут

### 1. Получи ключи

- **Finnhub** — зарегистрируйся на https://finnhub.io, скопируй API key (free tier).
- **Telegram bot** — напиши [@BotFather](https://t.me/BotFather), `/newbot`, скопируй токен. Запусти своего бота, потом открой `https://api.telegram.org/bot<TOKEN>/getUpdates` после `/start` в чате с ботом и скопируй `chat.id`.

### 2. Настрой конфиги

```bash
cp .env.example .env          # вставь FINNHUB_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
cp config.yaml.example config.yaml   # поправь watchlist и капитал
```

### 3. Подними Docker

```bash
docker compose up --build -d
docker compose logs -f bot
```

В Telegram должен прилететь `🐙 tentaclez online. /help`. Жми `/help`.

### 4. Загрузи стартовые позиции

Если уже что-то держишь в Robinhood — расскажи боту:
```
/add QQQ 0.1 510.40
/add XLE 1   85.20
```

Дальше `/portfolio` покажет live-стоимость и P&L.

## Команды Telegram

| Команда | Что делает |
|---|---|
| `/status` | состояние бота |
| `/watchlist` | конфигурированные тикеры |
| `/portfolio` | открытые позиции + live P&L |
| `/add SYMBOL QTY PRICE` | записать ручную покупку |
| `/sell SYMBOL QTY PRICE` | записать продажу (FIFO), посчитать профит-сплит |
| `/closed …` | алиас `/sell` |
| `/divcal` | ex-div календарь по watchlist |
| `/help` | справка |

## Логика сигналов (стартовая)

```
score = Σ wᵢ · ruleᵢ(price-data)

BUY  если score ≥ buy_threshold
SELL если score ≤ sell_threshold
HOLD иначе
```

Все веса и пороги — в `config.yaml`. Каждое правило вкладывает понятную причину в alert (`RSI 28 oversold`, `Golden cross 50/200`, `ex-div in 5d` и т.д.).

## Локальный dev (без Docker)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
python -m tentaclez
```

## Структура

```
src/tentaclez/
├── __main__.py          # вход + apscheduler + жизненный цикл
├── config.py            # pydantic-конфиги, .env, yaml
├── market_hours.py      # NYSE расписание, праздники
├── data/
│   ├── finnhub.py       # quote, dividends
│   └── yfinance_src.py  # история для индикаторов
├── indicators.py        # RSI, MACD, SMA, Bollinger, ATR
├── signal_engine.py     # композит → BUY/SELL/HOLD
├── engine.py            # SignalRunner: tick / brief / summary
├── portfolio.py         # SQLAlchemy: Lot, Trade, SignalLog
├── allocator.py         # профит-сплит
├── notify.py            # отправка в Telegram
└── commands.py          # /add /sell /portfolio /divcal …
```

## Дальше по плану

- **v0.2**: бэктест-CLI, Alpaca paper-trading как опциональный слой исполнения.
- **v0.3**: macro-фильтр (VIX, доходность 10y), DCA-режим для core-ETF.
- **v0.4**: web-дашборд (FastAPI), история сигналов и сделок.
- **v0.5**: live-исполнение через Alpaca с подтверждением в Telegram.

## Отказ от ответственности

Это образовательный инструмент. Сигналы не являются инвестиционным советом. Прибыль не гарантирована, можно потерять часть или всё. Тестируй на бумаге прежде чем рисковать живыми деньгами.
