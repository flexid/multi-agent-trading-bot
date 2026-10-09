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
