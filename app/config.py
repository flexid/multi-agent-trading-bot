"""Typed access to config.toml and .env. The only module that reads either."""

from __future__ import annotations

import tomllib
from datetime import datetime
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


class CatalystsConfig(_Section):
    """The catalyst agent for the alt sleeve (owner 2026-10-10): official project and alt-news
    X accounts, Bybit announcements and a token-unlock calendar, classified by Jev."""

    accounts: list[str] = Field(default_factory=list)  # X handles, read every cycle (billed)
    # handle -> asset: the project's own account; it promotes its coin, so its bullish events
    # count half, its promotional posts need more materiality, and two events per account count
    official: dict[str, str] = Field(default_factory=dict)
    reads_per_cycle_max: int = 30  # share of the daily X read budget
    shadow_model: str = "claude-sonnet-5-5"  # runs beside Jev while not live; "" turns it off
    unlock_slugs: dict[str, str] = Field(default_factory=dict)  # asset -> DefiLlama slug
    unlock_horizon_days: int = 30  # how far ahead unlocks are loaded


class AggressiveConfig(_Section):
    """What the admin's "aggressive mode" switch changes (owner 2026-10-11): more risk per
    trade and the full leverage ceiling in pursuit of the 1%-a-day goal, no locks of any
    kind (no day-loss stop, no drawdown pause, no automatic brake, no daily trade limit),
    a trade banked as soon as it has earned its share of the goal, and a short holding
    limit. Each trade keeps its own stop. Code keeps hard bounds on risk and leverage."""

    risk_multiplier: Decimal = Decimal("2.5")  # × each sleeve's risk per trade
    leverage_ceiling: int = 10  # instead of the 2x start ceiling; sleeve caps still apply
    net_beta_exposure_max: Decimal = Decimal("3.0")  # × equity, per sleeve and in total
    max_hold_hours: int = 48  # 24 until 2026-10-12: too short for a 4-hour setup
    # After the day goal is banked, start a decision cycle at once instead of waiting for
    # the next regular one, unless that one starts within the gap anyway.
    goal_cycle: bool = True
    goal_cycle_min_gap_minutes: int = Field(default=30, ge=5, le=240)
    # Stops sit this many times further from the entry than the managers put them (owner:
    # more room). The position shrinks to match, so the loss at the stop is unchanged.
    stop_room: Decimal = Decimal("1.5")
    # Close a trade once its net profit, after both fees and the borrow interest, reaches
    # this share of the ledger's equity. Checked every ten seconds, so it fills at or a
    # little above it ("between 1 and 2% net").
    take_profit_equity_pct: Decimal = Decimal("0.01")
    # Close a single trade once its net profit, the closing fee taken off, reaches this
    # share of the margin put in (owner 2026-10-12: "1-3% on a single trade"). 0 = off.
    take_profit_trade_pct: Decimal = Field(default=Decimal("0.02"), ge=0, le=Decimal("0.5"))
    # Past that level the take trails: the trade stays open while the return keeps rising
    # and is closed when it has given back this share of its best return, never below the
    # level itself (owner: "if it's suddenly 4% it can close on 4% as well").
    take_profit_giveback: Decimal = Field(default=Decimal("0.25"), ge=0, le=Decimal("0.9"))
    # The loss at the stop may be at most this share of the margin: the leverage, and so
    # the size, comes down until it fits (v10, owner: a 2% take needs a stop of about 4%
    # on margin, not 9%). 0 = off.
    stop_max_margin_pct: Decimal = Field(default=Decimal("0.04"), ge=0, le=Decimal("0.5"))


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
    # Which trade events are posted (owner 2026-10-15: twenty posts a day read as a bot):
    # "all" = opens and closes, "closes" = closes only, "wins" = profitable closes only.
    events: Literal["all", "closes", "wins"] = "closes"


class RiskConfig(_Section):
    maintenance_margin_rate: Decimal = Decimal("0.03")
    # The hard (touch) stop sits this many ATR beyond the soft, close-based one. v11: half
    # an ATR; a whole one pushed the hard stop 6-9% from the entry and the leverage to 1x.
    hard_stop_atr: Decimal = Field(default=Decimal("0.5"), ge=0, le=3)
    # No new trade in the direction of a move already larger than this many ATR (14 × 4h)
    # over the last 24 hours: that is chasing (owner 2026-10-12, logic v9). 0 switches it off.
    chase_max_atr: Decimal = Field(default=Decimal("2"), ge=0, le=10)


class MacroAgentConfig(_Section):
    fng_weight: float = 0.25
    dominance_weight: float = 0.0


class AgentsConfig(_Section):
    macro: MacroAgentConfig = MacroAgentConfig()
    # The chart agent's vision read (Opus, Sonnet for alts). Off since v10: it confirmed
    # the code's reading at a third of the agent budget. The code track runs regardless.
    chart_vision: bool = False
    # Per-asset overrides on the tuned agent weights, e.g. DOGE leans on X sentiment.
    weights_by_asset: dict[str, dict[str, float]] = Field(default_factory=dict)


class XConfig(_Section):
    accounts: list[str] = Field(default_factory=list)
    search_per_asset: int = 20
    reads_per_cycle_max: int = 90
    query_max_chars: int = 512  # X recent-search query length limit on this tier
    curated_per_query: int = 20  # posts asked per filtered query over the curated accounts


class ServerConfig(_Section):
    """The droplet the bot runs on; its metrics are read from DigitalOcean monitoring."""

    droplet_id: int | None = None
    keep_days: int = 14  # how long the 2-minute samples are kept


class ArenaTraderConfig(_Section):
    model: str  # the strategy author: an LLM id the llm layer knows
    allocation_usdt: Decimal = Decimal(0)  # informational; the sub-account's balance rules


class ArenaConfig(_Section):
    """The dorkbot arena (app/arena, competition-site/): three traders on the BTCUSDT
    perpetual under fixed rules. Its own process; off unless ``enabled``."""

    enabled: bool = False
    live: bool = False  # real orders; False = everything but the order calls (paper fills)
    market: str = "BTCUSDT"
    jev_model: str = "jev-1.13.0"  # pinned: a model bump mid-season changes every trader
    max_leverage: int = Field(default=10, ge=1, le=20)
    tp_net_roe_range: tuple[int, int] = (2, 10)
    losses_to_bench: int = Field(default=2, ge=1, le=10)
    # A round ends when all are benched, or after this many hours regardless (owner
    # 2026-10-14): a trader that never trades can never be benched and would hold the
    # round open for everyone. 0 = no limit.
    max_round_hours: int = Field(default=48, ge=0, le=720)
    taker_fee_pct: Decimal = Decimal("0.055")
    # Owner 2026-10-24 ("make it winnable"): two of three traders were gross positive and
    # fees took it all, a trade every two hours at 3-4x. No new entry within this long of
    # a close, and at most this many entries per trader per 24 hours.
    entry_cooldown_minutes: int = Field(default=60, ge=0, le=1440)
    max_entries_per_day: int = Field(default=4, ge=1, le=100)
    min_conviction_floor: float = Field(default=0.5, ge=0, le=1)
    margin_share: Decimal = Field(
        default=Decimal("0.95"), gt=0, le=1
    )  # of the sub-account's USDT per trade
    api_port: int = 8787
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "https://arena.dorkbot.dev",
            "https://dorkbot.dev",
            "https://dex.dorkbot.dev",
        ]
    )
    social: bool = False  # X posts for arena trades (bot spec §7); off until asked
    traders: dict[str, ArenaTraderConfig] = Field(default_factory=dict)  # kwikton, kwekton, kwakton


class DexConfig(_Section):
    """dorkbot on a DEX (app/dex, docs/DEX_PLAN.md): a mirror of the main book on
    Hyperliquid perpetuals. Off unless ``enabled``; ``mode`` paper | testnet | live."""

    enabled: bool = False
    mode: Literal["paper", "testnet", "live"] = "paper"
    follow_track: str = "primary"  # the book whose record is mirrored
    assets: list[str] = Field(default_factory=list)  # empty = whatever the venue lists
    paper_equity: Decimal = Decimal(1000)
    # Hyperliquid account abstraction: "unified" (one USDC balance for spot and perps, the
    # default for new accounts) reads the balance from the spot clearinghouse; "standard"
    # from the perps clearinghouse (2026-10-19: the owner's account is unified).
    account_mode: Literal["unified", "standard"] = "unified"
    taker_fee_pct: Decimal = Decimal("0.035")
    leverage_max: int = Field(default=10, ge=1, le=50)
    # The most margin one mirror trade may put up, whatever the equity (owner 2026-10-19:
    # the first live run is small by construction). 0 = no cap.
    max_margin_usdt: Decimal = Field(default=Decimal(25), ge=0)
    min_notional_usdt: Decimal = Decimal(10)  # Hyperliquid's minimum order value
    tick_seconds: int = Field(default=5, ge=2, le=60)
    publish_seconds: int = Field(default=15, ge=5, le=300)
    explorer: str = "https://app.hyperliquid.xyz/explorer"
    # The take order on the venue (owner 2026-10-21: off). On, the venue closes a winner on
    # touch before the main book's trailing take does and the mirror diverges; off, every
    # exit comes from the main book through the mirror, and only the stop rests on the venue.
    venue_take: bool = True
    # The public record starts here (owner 2026-10-21): trades opened before it stay in the
    # ledger and the admin but not in the feed, the stats or the index.
    record_since: datetime | None = None
    record_note: str = ""


class TriggersConfig(_Section):
    """Cycles outside the four-hour clock (v10: the bot reacts to the market, not to the
    hour). All in code; the managers are only called when something happened."""

    price_atr_1h: Decimal = Field(default=Decimal("1.5"), ge=Decimal("0.5"), le=10)
    max_per_day: int = Field(default=4, ge=0, le=24)  # triggered cycles, all sources
    min_gap_minutes: int = Field(default=30, ge=5, le=240)  # after any cycle
    x_poll_minutes: int = Field(default=3, ge=1, le=60)  # the curated accounts, Jev labels
    light_cycle: bool = True  # the hourly code-only check that can trigger a full cycle
    formula_flip: float = Field(default=0.3, ge=0.1, le=1.0)  # |score| that counts as a call


class SiteConfig(_Section):
    public_domain: str = "dorkbot.dev"
    admin_domain: str = "admin.dorkbot.dev"
    snapshot_interval_minutes: int = 5
    live_interval_seconds: int = Field(default=15, ge=5, le=300)  # open trades, price now
    # The public record starts here (owner 2026-10-19): the index rebases to 100 at this
    # moment and earlier trades stay in the ledger and the admin. None = from the first
    # equity point. Changing it moves the line; nothing is deleted.
    record_since: datetime | None = None
    record_note: str = ""  # shown next to the performance heading


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
    catalysts: str = "jev-latest"  # the catalyst classifier (owner 2026-10-10)
    optima: str = "claude-opus-5-5"  # weekly cost and execution adviser (owner 2026-10-10)


class ModelsConfig(TaskModels):
    shadow_alt: TaskModels | None = None
    # The third manager seat (owner 2026-10-11, logic v6): a trade needs two of three with
    # nobody opposite. "" runs the bot with two seats, where both must agree.
    pm_3: str = ""


class LLMConfig(_Section):
    # The managers' shadow swap (each seat once more on the other provider, logged as
    # variant "alt"). Off since v10: it cost more than the three real seats and answered
    # its question (both providers scored the same).
    shadow_managers: bool = False
    timeout_s: float = 120
    max_output_tokens: int = 4000
    effort: str = "medium"
    jev_timeout_s: float = 20  # Jev answers in well under a second; fail closed early
    timeout_s_by_provider: dict[str, float] = Field(default_factory=dict)  # e.g. xai = 150
    pm_3_deadline_s: float = 480  # the third seat's answer is dropped after this; two decide
    pm_3_fallback_effort: str = ""  # one quick retry at this effort when the first try fails
    pm_3_fallback_timeout_s: float = 150
    effort_by_provider: dict[str, str] = Field(default_factory=dict)  # e.g. xai = "low"
    # USD per million tokens: [input, output] or [input, output, cached input]
    pricing: dict[str, tuple[Decimal, ...]] = Field(default_factory=dict)


class Config(_Section):
    llm: LLMConfig = LLMConfig()
    trading: TradingConfig
    pilot: PilotConfig = PilotConfig()
    aggressive: AggressiveConfig = AggressiveConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    posting: PostingConfig
    risk: RiskConfig = RiskConfig()
    agents: AgentsConfig = AgentsConfig()
    x: XConfig = XConfig()
    catalysts: CatalystsConfig = CatalystsConfig()
    site: SiteConfig = SiteConfig()
    triggers: TriggersConfig = TriggersConfig()
    arena: ArenaConfig = ArenaConfig()
    dex: DexConfig = DexConfig()
    server: ServerConfig = ServerConfig()
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

    jev_api_key: SecretStr = SecretStr("")  # TypeSafe Jev; scheduler only, never the admin
    do_access_token: SecretStr = SecretStr("")  # DigitalOcean; scheduler only, never the admin
    x_ai_api_key: SecretStr = SecretStr("")  # xAI (Grok): the shadow third manager seat
    # the arena's three AI subaccounts (make bybit-authorize ARGS='--create --for kwikton')
    competition_kwikton_api_key: SecretStr = SecretStr("")
    competition_kwikton_api_secret: SecretStr = SecretStr("")
    competition_kwekton_api_key: SecretStr = SecretStr("")
    competition_kwekton_api_secret: SecretStr = SecretStr("")
    competition_kwakton_api_key: SecretStr = SecretStr("")
    competition_kwakton_api_secret: SecretStr = SecretStr("")
    # the DEX mirror (app/dex): the account's address and an API (agent) key that may trade
    # but never withdraw; empty in paper mode
    dex_address: str = ""
    dex_private_key: SecretStr = SecretStr("")
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
