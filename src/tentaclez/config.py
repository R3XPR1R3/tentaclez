from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    """Secrets and runtime knobs from env / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/tentaclez.db", alias="DATABASE_URL"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )
    tz: str = Field(default="America/New_York", alias="TZ")
    config_path: str = Field(default="config.yaml", alias="CONFIG_PATH")


class TickerCfg(BaseModel):
    """Per-ticker ladder parameters. All fractions, e.g. 0.02 = 2%."""

    symbol: str
    dip_percent: float = 0.02
    profit_percent: float = 0.04
    check_frequency_minutes: int = 10  # ignored at the moment; global schedule wins


class TradeSizingCfg(BaseModel):
    min_trade_pct: float = 0.10  # of free cash
    max_trade_pct: float = 0.20

    @field_validator("max_trade_pct")
    @classmethod
    def _max_ge_min(cls, v: float, info) -> float:
        mn = info.data.get("min_trade_pct", 0)
        if v < mn:
            raise ValueError("max_trade_pct must be >= min_trade_pct")
        return v


class AllocationCfg(BaseModel):
    """When you record a winning /sell, the bot suggests how to split realized profit."""

    reinvest_trade_pct: float = 80
    dividend_etf_pct: float = 20
    dividend_etf_symbols: list[str] = ["SCHD", "JEPI"]

    @field_validator("dividend_etf_pct")
    @classmethod
    def _sum_to_100(cls, v: float, info) -> float:
        total = info.data.get("reinvest_trade_pct", 0) + v
        if abs(total - 100) > 0.01:
            raise ValueError(f"reinvest_trade_pct + dividend_etf_pct must = 100, got {total}")
        return v


class ScheduleCfg(BaseModel):
    """All times in America/New_York."""

    pre_open_brief: str = "09:00"
    post_close_summary: str = "16:30"
    intraday_check_minutes: int = 10  # poll cadence during the session


class AnalyticsCfg(BaseModel):
    """Future-extension hooks (per spec section 7). Off by default at small capital."""

    enabled: bool = False
    sma_long_period: int = 200    # if price < SMA200 → tighten or pause buys
    vix_pause_threshold: float = 30.0


class Config(BaseModel):
    tickers: list[TickerCfg] = []
    sizing: TradeSizingCfg = TradeSizingCfg()
    allocation: AllocationCfg = AllocationCfg()
    schedule: ScheduleCfg = ScheduleCfg()
    analytics: AnalyticsCfg = AnalyticsCfg()

    def ticker(self, symbol: str) -> TickerCfg | None:
        s = symbol.upper()
        for t in self.tickers:
            if t.symbol.upper() == s:
                return t
        return None

    def upsert_ticker(self, t: TickerCfg) -> None:
        s = t.symbol.upper()
        for i, existing in enumerate(self.tickers):
            if existing.symbol.upper() == s:
                self.tickers[i] = t
                return
        self.tickers.append(t)

    def watchlist(self) -> list[str]:
        return [t.symbol.upper() for t in self.tickers]


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    env = Env()
    cfg_path = Path(path or env.config_path)
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"config not found at {cfg_path}. Copy config.yaml.example to config.yaml."
        )
    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)
