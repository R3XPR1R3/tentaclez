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

    finnhub_api_key: str = Field(default="", alias="FINNHUB_API_KEY")
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


class PortfolioCfg(BaseModel):
    total_capital_usd: float = 100
    max_position_pct: float = 25
    min_position_usd: float = 10


class AllocationCfg(BaseModel):
    reinvest_core_pct: float = 50
    reinvest_dividend_pct: float = 30
    cash_buffer_pct: float = 20

    @field_validator("cash_buffer_pct")
    @classmethod
    def _sum_to_100(cls, v: float, info) -> float:
        data = info.data
        total = data.get("reinvest_core_pct", 0) + data.get("reinvest_dividend_pct", 0) + v
        if abs(total - 100) > 0.01:
            raise ValueError(f"allocation percentages must sum to 100, got {total}")
        return v


class ScheduleCfg(BaseModel):
    pre_open_brief: str = "09:00"
    post_close_summary: str = "16:30"
    intraday_check_minutes: int = 5
    dividend_calendar_horizon_days: int = 14


class WatchlistCfg(BaseModel):
    core: list[str] = []
    growth: list[str] = []
    dividend: list[str] = []
    sector: list[str] = []

    def all_tickers(self) -> list[str]:
        seen: dict[str, None] = {}
        for bucket in (self.core, self.growth, self.dividend, self.sector):
            for t in bucket:
                seen.setdefault(t.upper(), None)
        return list(seen)

    def bucket_of(self, ticker: str) -> str:
        t = ticker.upper()
        if t in {x.upper() for x in self.core}:
            return "core"
        if t in {x.upper() for x in self.growth}:
            return "growth"
        if t in {x.upper() for x in self.dividend}:
            return "dividend"
        if t in {x.upper() for x in self.sector}:
            return "sector"
        return "unknown"


class RuleCfg(BaseModel):
    enabled: bool = True
    weight: float = 0


class RsiRule(RuleCfg):
    period: int = 14
    oversold: float = 30
    overbought: float = 70
    weight: float = 25


class MacdRule(RuleCfg):
    fast: int = 12
    slow: int = 26
    signal: int = 9
    weight: float = 20


class SmaCrossRule(RuleCfg):
    fast: int = 50
    slow: int = 200
    weight: float = 30


class BollingerRule(RuleCfg):
    period: int = 20
    std: float = 2.0
    weight: float = 15


class DividendCaptureRule(RuleCfg):
    days_before_exdate: int = 7
    min_yield_pct: float = 2.0
    weight: float = 10


class SignalsCfg(BaseModel):
    buy_threshold: float = 40
    sell_threshold: float = -40
    rsi: RsiRule = RsiRule()
    macd: MacdRule = MacdRule()
    sma_cross: SmaCrossRule = SmaCrossRule()
    bollinger: BollingerRule = BollingerRule()
    dividend_capture: DividendCaptureRule = DividendCaptureRule()


class RiskCfg(BaseModel):
    atr_period: int = 14
    stop_atr_mult: float = 2.0
    target_atr_mult: float = 3.0


class Config(BaseModel):
    portfolio: PortfolioCfg = PortfolioCfg()
    allocation: AllocationCfg = AllocationCfg()
    schedule: ScheduleCfg = ScheduleCfg()
    watchlist: WatchlistCfg = WatchlistCfg()
    signals: SignalsCfg = SignalsCfg()
    risk: RiskCfg = RiskCfg()


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
