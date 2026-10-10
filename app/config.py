"""Typed access to config.toml and .env. The only module that reads either."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
ENV_PATH = ROOT / ".env"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TradingConfig(_Section):
    assets: list[str]
    capital_max_usdt: Decimal
    live_start_fraction: Decimal = Decimal("0.10")
    live_allowed: bool
    leverage_max: int
    risk_per_trade: Decimal
    # The max shadow track sizes with this instead (owner goal 2026-10-09: show the
    # leveraged variant beside the go-live record).
    risk_per_trade_max: Decimal = Decimal("0.025")
    capital_share_per_asset: Decimal
    gross_exposure_max: Decimal
    # Beta-weighted same-direction exposure cap, × equity (owner 2026-10-10)
    net_beta_exposure_max: Decimal = Decimal("1.5")
    holding_min_hours: int
    holding_max_days: int
    short_allowed: bool
    day_loss_stop: Decimal
    drawdown_pause: Decimal
    emergency_brake: Decimal
    depth_cap: Decimal
    cycle_hours: int


class SleeveConfig(_Section):
    """One sleeve of the book (owner 2026-10-10): its assets, share of capital and its own
    risk budget, caps and day-loss stop; optional agent-weight and model overrides."""

    assets: list[str]
    capital_fraction: Decimal
    risk_per_trade: Decimal
    leverage_max: int
    net_beta_exposure_max: Decimal = Decimal("1.5")
    day_loss_stop: Decimal = Decimal("-0.02")
    weights: dict[str, float] = Field(default_factory=dict)  # agent weight overrides
    models: dict[str, str] = Field(default_factory=dict)  # task -> model overrides


class PilotConfig(_Section):
    enabled: bool = False
    capital_usdt: Decimal = Decimal(200)


class ExchangeConfig(_Section):
    quote: str = "USDC"
    alt_quote: str | None = None
    recv_window_ms: int = 5000
    base_coin: dict[str, str] = Field(default_factory=dict)
    extra_candle_symbols: list[str] = Field(default_factory=list)  # e.g. ETHBTC, SOLBTC
    price_feed: Literal["ws", "rest"] = "rest"  # executor quotes; go-live requires "ws"
    backup_stop: bool = True  # exchange-side stop behind every real position


class PostingConfig(_Section):
    enabled: bool
    handle: str
    language: str
    style: str
    delay_minutes: tuple[int, int]
    max_posts_per_day: int
    post_in_shadow: bool
    cashtags: dict[str, str]
    # A close with at least this margin result gets the trade card and an openly pleased
    # post (owner, 2026-10-09).
    highlight_margin_pct: Decimal = Decimal("0.10")


class RiskConfig(_Section):
    maintenance_margin_rate: Decimal = Decimal("0.03")


class MacroAgentConfig(_Section):
    fng_weight: float = 0.25
    dominance_weight: float = 0.0


class AgentsConfig(_Section):
    macro: MacroAgentConfig = MacroAgentConfig()
    # Per-asset overrides on the tuned agent weights, e.g. DOGE leans on X sentiment.
    weights_by_asset: dict[str, dict[str, float]] = Field(default_factory=dict)


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


class TaskModels(_Section):
    pm_1: str
    pm_2: str
    macro: str
    chart_patterns: str
    indicators: str
    polymarket: str
    x_sentiment: str
    post_writer: str
    post_auditor: str
    memo: str = "claude-opus-5-5"  # weekly improvement memo (owner 2026-10-09)


class ModelsConfig(TaskModels):
    shadow_alt: TaskModels | None = None


class LLMConfig(_Section):
    timeout_s: float = 120
    max_output_tokens: int = 4000
    effort: str = "medium"
    pricing: dict[str, tuple[Decimal, Decimal]] = Field(default_factory=dict)


class Config(_Section):
    llm: LLMConfig = LLMConfig()
    trading: TradingConfig
    pilot: PilotConfig = PilotConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    posting: PostingConfig
    risk: RiskConfig = RiskConfig()
    agents: AgentsConfig = AgentsConfig()
    x: XConfig = XConfig()
    site: SiteConfig = SiteConfig()
    budget: BudgetConfig
    models: ModelsConfig
    sleeves: dict[str, SleeveConfig] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        # With sleeves, the traded universe is their union, in sleeve order.
        if self.sleeves:
            assets = [a for s in self.sleeves.values() for a in s.assets]
            object.__setattr__(self.trading, "assets", assets)

    def sleeve_of(self, asset: str) -> str | None:
        for name, s in self.sleeves.items():
            if asset in s.assets:
                return name
        return None

    def sleeve_cfg(self, asset: str) -> SleeveConfig | None:
        name = self.sleeve_of(asset)
        return self.sleeves[name] if name else None

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
    coingecko_api_key: SecretStr = SecretStr("")

    cloudflare_account_id: str = ""
    cloudflare_api_token: SecretStr = SecretStr("")
    r2_bucket: str = "dorkbot-public"
    r2_backup_bucket: str = ""  # private bucket for pg_dump files; empty = keep on disk only
    r2_access_key_id: SecretStr = SecretStr("")
    r2_secret_access_key: SecretStr = SecretStr("")
    snapshot_public_url: str = ""

    # Admin (M8b): session signing key and alert email provider
    admin_secret_key: SecretStr = SecretStr("")
    alert_email_to: str = ""
    alert_email_from: str = ""
    resend_api_key: SecretStr = SecretStr("")
    smtp_url: str = ""

    database_url: SecretStr = SecretStr("")
    test_database_url: SecretStr = SecretStr("")


def load_config(path: Path = CONFIG_PATH) -> Config:
    with path.open("rb") as fh:
        return Config.model_validate(tomllib.load(fh))


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()
