"""Typed access to config.toml and .env. The only module that reads either."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
ENV_PATH = ROOT / ".env"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TradingConfig(_Section):
    assets: list[str]
    capital_max_usdc: Decimal
    live_allowed: bool
    leverage_max: int
    leverage_max_spx6900: int
    risk_per_trade: Decimal
    capital_share_per_asset: Decimal
    gross_exposure_max: Decimal
    holding_min_hours: int
    holding_max_days: int
    short_allowed: bool
    day_loss_stop: Decimal
    drawdown_pause: Decimal
    emergency_brake: Decimal
    depth_cap: Decimal
    cycle_hours: int


class ExchangeConfig(_Section):
    quote: str = "USDC"
    alt_quote: str | None = None
    recv_window_ms: int = 5000
    base_coin: dict[str, str] = Field(default_factory=dict)


class PostingConfig(_Section):
    enabled: bool
    handle: str
    language: str
    style: str
    delay_minutes: tuple[int, int]
    max_posts_per_day: int
    post_in_shadow: bool
    cashtags: dict[str, str]


class XConfig(_Section):
    accounts: list[str] = Field(default_factory=list)
    search_per_asset: int = 20
    reads_per_cycle_max: int = 90


class SiteConfig(_Section):
    public_domain: str = "dorkbot.dev"
    admin_domain: str = "admin.dorkbot.dev"
    snapshot_interval_minutes: int = 5


class BudgetConfig(_Section):
    api_usd_per_month: Decimal
    x_reads_per_day: int


class ModelsConfig(_Section):
    pm_1: str
    pm_2: str
    macro: str
    chart_patterns: str
    indicators: str
    polymarket: str
    x_sentiment: str
    post_writer: str
    post_auditor: str


class LLMConfig(_Section):
    timeout_s: float = 120
    max_output_tokens: int = 4000
    effort: str = "medium"
    pricing: dict[str, tuple[Decimal, Decimal]] = Field(default_factory=dict)


class Config(_Section):
    llm: LLMConfig = LLMConfig()
    trading: TradingConfig
    exchange: ExchangeConfig = ExchangeConfig()
    posting: PostingConfig
    x: XConfig = XConfig()
    site: SiteConfig = SiteConfig()
    budget: BudgetConfig
    models: ModelsConfig

    def base_coin(self, asset: str) -> str:
        return self.exchange.base_coin.get(asset, asset)

    def symbol(self, asset: str, quote: str | None = None) -> str:
        return f"{self.base_coin(asset)}{quote or self.exchange.quote}"


class Secrets(BaseSettings):
    """Values from .env. SecretStr keeps them out of reprs and logs."""

    model_config = SettingsConfigDict(env_file=ENV_PATH, extra="ignore")

    bybit_api_key: SecretStr = SecretStr("")
    bybit_api_secret: SecretStr = SecretStr("")
    bybit_base_url: str = "https://api.bybit.com"

    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")

    x_api_key: SecretStr = SecretStr("")
    x_api_secret: SecretStr = SecretStr("")
    x_access_token: SecretStr = SecretStr("")
    x_access_token_secret: SecretStr = SecretStr("")
    x_bearer_token: SecretStr = SecretStr("")

    fred_api_key: SecretStr = SecretStr("")

    database_url: SecretStr = SecretStr("")


def load_config(path: Path = CONFIG_PATH) -> Config:
    with path.open("rb") as fh:
        return Config.model_validate(tomllib.load(fh))


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()
