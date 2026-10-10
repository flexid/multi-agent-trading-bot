"""SQLAlchemy models. Every table the bot writes to lives here; Alembic owns the DDL."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Wide enough for satoshis of BTC and for memecoin prices with 8 decimals.
Money = Numeric(28, 10)


class Base(DeclarativeBase):
    type_annotation_map = {Decimal: Money, dict[str, Any]: JSONB, list[Any]: JSONB}


class Candle(Base):
    """OHLCV bar as Bybit reports it. Only closed bars are stored."""

    __tablename__ = "candles"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    interval: Mapped[str] = mapped_column(String(4), primary_key=True)  # 15, 60, 240, D
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal]
    high: Mapped[Decimal]
    low: Mapped[Decimal]
    close: Mapped[Decimal]
    volume: Mapped[Decimal]
    turnover: Mapped[Decimal]
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderBookSnapshot(Base):
    __tablename__ = "orderbook_snapshots"
    __table_args__ = (Index("ix_orderbook_symbol_ts", "symbol", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # exchange timestamp
    mid: Mapped[Decimal]
    spread_bps: Mapped[Decimal]
    depth_bid_2pct: Mapped[Decimal]  # quote value resting within -2% of mid
    depth_ask_2pct: Mapped[Decimal]
    depth_truncated: Mapped[bool] = mapped_column(Boolean, default=False)  # 200 levels < ±2%
    bids: Mapped[list[Any]]  # [[price, qty], ...] top levels only
    asks: Mapped[list[Any]]
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AccountSnapshot(Base):
    """Balances, borrows and open orders as the exchange reports them; reconciliation input."""

    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    total_equity: Mapped[Decimal | None]
    quote_balance: Mapped[Decimal]
    coins: Mapped[list[Any]]  # [{coin, wallet_balance, borrow_amount, usd_value}, ...]
    open_orders: Mapped[list[Any]]


class PolymarketMarket(Base):
    """Market metadata from Gamma. ``question`` is untrusted text: for the dashboard and
    the Polymarket agent's mapping prompt only, never for the PMs."""

    __tablename__ = "polymarket_markets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(80))
    slug: Mapped[str] = mapped_column(String(200))
    question: Mapped[str] = mapped_column(Text)
    outcomes: Mapped[list[Any]]
    token_ids: Mapped[list[Any]]
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean)
    closed: Mapped[bool] = mapped_column(Boolean)
    asset: Mapped[str | None] = mapped_column(String(10), index=True)  # set by the mapper
    # {"threshold": 84000, "direction": "above"|"below", "kind": "price"|"other"}; model output
    # validated by app/agents/polymarket_map.py, never free text
    mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    mapped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PolymarketPrice(Base):
    __tablename__ = "polymarket_prices"

    market_id: Mapped[str] = mapped_column(
        ForeignKey("polymarket_markets.id", ondelete="CASCADE"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    prices: Mapped[list[Any]]  # one Decimal-as-string per outcome
    volume_24h: Mapped[Decimal]
    liquidity: Mapped[Decimal | None]


class PerpMetric(Base):
    """Funding and open interest from Bybit global perpetuals; indicator input only."""

    __tablename__ = "perp_metrics"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    funding_rate: Mapped[Decimal]
    open_interest_value: Mapped[Decimal]
    mark_price: Mapped[Decimal]


class MacroObservation(Base):
    """One value of one series (FRED or other) per observation date."""

    __tablename__ = "macro_observations"

    series: Mapped[str] = mapped_column(String(40), primary_key=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    value: Mapped[Decimal]
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class XPostRecord(Base):
    """A read X post with the X agent's labels. ``text`` is raw, untrusted input: only the
    X agent reads it; the public site and the PMs only ever see the labels."""

    __tablename__ = "x_posts"
    __table_args__ = (Index("ix_x_posts_asset_created", "asset", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    author_id: Mapped[str | None] = mapped_column(String(32))
    author: Mapped[str | None] = mapped_column(String(64))  # handle when known (curated list)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    query_asset: Mapped[str | None] = mapped_column(String(10))  # which search found it
    text: Mapped[str] = mapped_column(Text)
    asset: Mapped[str | None] = mapped_column(String(10))  # label: a configured asset or none
    stance: Mapped[str | None] = mapped_column(String(10))  # bullish/bearish/neutral
    kind: Mapped[str | None] = mapped_column(String(10))  # news/analysis/shill/other
    credibility: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    shock: Mapped[bool | None] = mapped_column(Boolean)  # market-moving news
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LLMCall(Base):
    """One model call: what was asked (sizes only), what came back, what it cost."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_task_ts", "task", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    task: Mapped[str] = mapped_column(String(40))
    asset: Mapped[str | None] = mapped_column(String(10))
    cycle_id: Mapped[int | None] = mapped_column(BigInteger)
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(60))
    prompt_name: Mapped[str] = mapped_column(String(40))
    prompt_version: Mapped[int] = mapped_column(Integer)
    input_chars: Mapped[int] = mapped_column(Integer)
    images: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Cycle(Base):
    """One decision cycle: scheduled every 4 hours or triggered (SPEC §7)."""

    __tablename__ = "cycles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(12))  # scheduled | triggered
    trigger: Mapped[str | None] = mapped_column(String(80))
    mode: Mapped[str] = mapped_column(String(8))  # shadow | live
    status: Mapped[str] = mapped_column(String(12))  # running | done | failed
    error: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)
    logic_version: Mapped[int | None] = mapped_column(Integer)  # app/version.py at the time
    # Beta-weighted exposure at the time of the cycle: betas, per-track net and gross × equity
    beta_exposure: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class AgentOutputRecord(Base):
    __tablename__ = "agent_outputs"
    __table_args__ = (Index("ix_agent_outputs_cycle", "cycle_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"))
    agent: Mapped[str] = mapped_column(String(20))
    asset: Mapped[str] = mapped_column(String(10))
    variant: Mapped[str] = mapped_column(String(10), default="main")  # main | alt (shadow swap)
    score: Mapped[float]
    confidence: Mapped[float]
    horizon: Mapped[str] = mapped_column(String(6))
    evidence: Mapped[list[Any]]
    risk_flags: Mapped[list[Any]]
    data_age_min: Mapped[int] = mapped_column(Integer)
    valid: Mapped[bool] = mapped_column(Boolean)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    components: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class PMProposalRecord(Base):
    """What one portfolio manager proposed for one asset, exactly as validated."""

    __tablename__ = "pm_proposals"
    __table_args__ = (Index("ix_pm_proposals_cycle", "cycle_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"))
    pm: Mapped[str] = mapped_column(String(10))  # pm_1 | pm_2
    variant: Mapped[str] = mapped_column(String(10), default="main")
    model: Mapped[str] = mapped_column(String(60))
    asset: Mapped[str] = mapped_column(String(10))
    direction: Mapped[str] = mapped_column(String(6))  # long | flat | short
    entry_low: Mapped[Decimal | None]
    entry_high: Mapped[Decimal | None]
    stop: Mapped[Decimal | None]
    target: Mapped[Decimal | None]
    max_hold_hours: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[float]
    conviction: Mapped[float]
    reasons: Mapped[list[Any]]
    weighted_up: Mapped[list[Any]]
    weighted_down: Mapped[list[Any]]


class DecisionRecord(Base):
    """The consensus per asset per cycle, before the risk engine (M5) sizes it."""

    __tablename__ = "decisions"
    __table_args__ = (Index("ix_decisions_asset_cycle", "asset", "cycle_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"))
    asset: Mapped[str] = mapped_column(String(10))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    spot: Mapped[Decimal | None]
    valid_agents: Mapped[int] = mapped_column(Integer)
    formula_score: Mapped[float | None]
    pm1_direction: Mapped[str | None] = mapped_column(String(6))
    pm2_direction: Mapped[str | None] = mapped_column(String(6))
    agreement: Mapped[str] = mapped_column(String(10))  # agree | partial | opposite | failed
    consensus_score: Mapped[float | None]
    direction: Mapped[str] = mapped_column(String(6))  # long | flat | short
    conviction: Mapped[float | None]
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # merged entry/stop/target/hold
    reason: Mapped[str] = mapped_column(String(120))
    risk_rule_hits: Mapped[list[Any]] = mapped_column(JSONB, default=list)  # filled by M5
    action: Mapped[str] = mapped_column(
        String(16), default="none"
    )  # none|open|rejected|borrow_failed


class Position(Base):
    """An open or closed position, paper or live. One row per trade from open to close."""

    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_status_asset", "status", "asset"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: Mapped[int | None] = mapped_column(BigInteger)
    cycle_id: Mapped[int | None] = mapped_column(BigInteger)
    mode: Mapped[str] = mapped_column(String(8))  # paper | pilot | live
    track: Mapped[str] = mapped_column(String(8), default="primary")  # primary | max | pilot
    asset: Mapped[str] = mapped_column(String(10))
    symbol: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(6))  # long | short
    sleeve: Mapped[str | None] = mapped_column(String(16))  # at open; None for the old universe
    status: Mapped[str] = mapped_column(String(10))  # pending | open | closing | closed
    qty: Mapped[Decimal]
    entry_price: Mapped[Decimal | None]
    exit_price: Mapped[Decimal | None]
    leverage: Mapped[Decimal] = mapped_column(Numeric(4, 1))
    margin: Mapped[Decimal]
    notional: Mapped[Decimal]
    borrowed: Mapped[Decimal] = mapped_column(default=0)  # base coin (short) or quote (long)
    stop: Mapped[Decimal]  # soft stop: fires on a 15-minute close beyond it
    hard_stop: Mapped[Decimal | None]  # touch stop an ATR further; sizing and backup use it
    target: Mapped[Decimal]
    trail_stop: Mapped[Decimal | None]
    # Stopped out, and price then reached the target within the planned hold: a wick-out.
    wick_out: Mapped[bool | None] = mapped_column(Boolean)
    max_hold_hours: Mapped[int] = mapped_column(Integer)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(String(20))
    fees: Mapped[Decimal] = mapped_column(default=0)
    interest: Mapped[Decimal] = mapped_column(default=0)
    pnl: Mapped[Decimal | None]  # net, quote
    pnl_price_pct: Mapped[Decimal | None]
    pnl_margin_pct: Mapped[Decimal | None]
    liquidation_price: Mapped[Decimal | None]
    order_link_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    x_post_id: Mapped[str | None] = mapped_column(String(32))  # M7
    atr: Mapped[Decimal | None]  # ATR(14, 4h) at entry; sizes the backup stop's offset
    # Exchange-side backup stop (real orders only): the trigger it rests at and its
    # orderLinkId. The link stays on a closed row until the cancel is confirmed.
    backup_stop_price: Mapped[Decimal | None]
    backup_stop_link: Mapped[str | None] = mapped_column(String(36))
    # An exit in progress: what has filled so far. Booked once, when the position is flat.
    exit_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    exit_filled_qty: Mapped[Decimal] = mapped_column(default=0, server_default="0")
    exit_value: Mapped[Decimal] = mapped_column(default=0, server_default="0")  # quote
    exit_fee: Mapped[Decimal] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderRecord(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_position", "position_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(8))
    order_link_id: Mapped[str] = mapped_column(String(36), unique=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(40))
    side: Mapped[str] = mapped_column(String(4))
    purpose: Mapped[str] = mapped_column(String(10))  # entry | exit
    qty: Mapped[Decimal]
    price: Mapped[Decimal]
    status: Mapped[str] = mapped_column(String(12))  # new | filled | cancelled | rejected
    filled_qty: Mapped[Decimal] = mapped_column(default=0)
    avg_price: Mapped[Decimal | None]
    fee: Mapped[Decimal] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class PaperAccount(Base):
    """Shadow ledgers: id 1 = primary track (live rules, 2x ceiling), id 2 = max-leverage
    comparison track. Go-live criteria (M9) read the primary only."""

    __tablename__ = "paper_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    track: Mapped[str] = mapped_column(String(8), default="primary")
    starting_capital: Mapped[Decimal]
    cash: Mapped[Decimal]  # free quote
    equity: Mapped[Decimal]  # cash + open positions' margin ± unrealized
    realized_pnl: Mapped[Decimal] = mapped_column(default=0)
    fees_paid: Mapped[Decimal] = mapped_column(default=0)
    interest_paid: Mapped[Decimal] = mapped_column(default=0)
    liquidations: Mapped[int] = mapped_column(Integer, default=0)
    day_start_equity: Mapped[Decimal]
    day_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    day_high_equity: Mapped[Decimal]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    mode: Mapped[str] = mapped_column(
        String(16), primary_key=True
    )  # live | paper:primary | paper:max
    equity: Mapped[Decimal]
    cash: Mapped[Decimal]
    open_positions: Mapped[int] = mapped_column(Integer)
    gross_exposure: Mapped[Decimal]


class ControlRequest(Base):
    """Kill switch, pause, resume: written by the admin or CLI, applied by the executor."""

    __tablename__ = "control_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(12))  # kill | pause | resume
    source: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(Text)


class Heartbeat(Base):
    __tablename__ = "heartbeats"

    process: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(String(200))


class XPostOut(Base):
    """A post the bot wrote (or would write in dry-run), with its audit trail."""

    __tablename__ = "x_posts_out"
    __table_args__ = (Index("ix_x_posts_out_position", "position_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(10))  # open | close | summary | test
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(10))  # writer | template
    audit_ok: Mapped[bool] = mapped_column(Boolean)
    audit_notes: Mapped[str | None] = mapped_column(Text)
    whitelist_ok: Mapped[bool] = mapped_column(Boolean)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    x_id: Mapped[str | None] = mapped_column(String(32))
    reply_to_x_id: Mapped[str | None] = mapped_column(String(32))
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdminUser(Base):
    """Single admin user (M8b): argon2 password hash, TOTP secret, lockout state."""

    __tablename__ = "admin_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    username: Mapped[str] = mapped_column(String(40))
    password_hash: Mapped[str] = mapped_column(String(200))
    totp_secret: Mapped[str] = mapped_column(String(64))
    totp_enrolled: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    known_ips: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actor: Mapped[str] = mapped_column(String(40))
    ip: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)


class ParamChange(Base):
    """Parameter changes requested from the admin; the executor applies them within hard
    bounds in code and records the outcome."""

    __tablename__ = "param_changes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    key: Mapped[str] = mapped_column(String(60))
    old_value: Mapped[str | None] = mapped_column(String(200))
    new_value: Mapped[str] = mapped_column(String(200))
    requested_by: Mapped[str] = mapped_column(String(40))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(String(200))


class AgentWeights(Base):
    """Formula weights after each tuning step (SPEC §7.7); the latest row is current."""

    __tablename__ = "agent_weights"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    weights: Mapped[dict[str, Any]] = mapped_column(JSONB)
    basis: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class RiskState(Base):
    """One row: the risk engine's memory across cycles (SPEC §8 limits)."""

    __tablename__ = "risk_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    starting_capital: Mapped[Decimal | None]  # set at go-live; emergency-brake reference
    peak_equity: Mapped[Decimal | None]
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_count_30d: Mapped[int] = mapped_column(Integer, default=0)
    last_pause_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    half_risk_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    emergency_brake: Mapped[bool] = mapped_column(Boolean, default=False)
    brake_reason: Mapped[str | None] = mapped_column(Text)
    day_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    leverage_ceiling: Mapped[Decimal] = mapped_column(Numeric(4, 1), default=2)  # ramps per §8
    mode: Mapped[str] = mapped_column(String(8), default="shadow")  # shadow | live (M9 decides)
    live_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Capital ramp after go-live: the share of capital_max_usdt in use and when it last moved.
    capital_fraction: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    capital_step_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    live_selftest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_golive_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    golive_report: Mapped[Any | None] = mapped_column(JSONB)  # list (book) or {book, sleeves}
    stop_buffer_atr: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))  # wick-out tuning
    # Sleeves (owner 2026-10-10): {sleeve: "shadow"|"live"} and {sleeve: iso-until} day locks
    sleeve_modes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    day_locked_sleeves: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    hard_stop_atr: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RiskRuleHit(Base):
    __tablename__ = "risk_rule_hits"
    __table_args__ = (Index("ix_risk_rule_hits_cycle", "cycle_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int | None] = mapped_column(BigInteger)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    asset: Mapped[str | None] = mapped_column(String(10))
    rule: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text)
    effect: Mapped[str] = mapped_column(String(20))  # cap | block | close_all | pause | brake


class SuppressedTrigger(Base):
    """A trigger that would have fired but hit the daily cap (owner 2026-10-10): logged
    with the price move that followed, to judge after shadow whether more triggered
    cycles are worth paying for. Logging only; decision logic unchanged."""

    __tablename__ = "suppressed_triggers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str] = mapped_column(Text)
    asset: Mapped[str | None] = mapped_column(String(10))
    spot: Mapped[Decimal | None]  # the asset's price at the time (BTC for market-wide)
    move_4h_pct: Mapped[float | None]
    move_1d_pct: Mapped[float | None]


class DataSource(Base):
    """Freshness per source; the agents refuse inputs older than 30 minutes (SPEC §6)."""

    __tablename__ = "data_sources"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)


class FetchRun(Base):
    __tablename__ = "fetch_runs"
    __table_args__ = (Index("ix_fetch_runs_source_started", "source", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool | None] = mapped_column(Boolean)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
