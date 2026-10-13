"""the arena's ledger: traders, rounds, rulebooks, decisions, trades, bench events, equity

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-10-14 00:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "arena_traders",
        sa.Column("id", sa.String(length=10), nullable=False),
        sa.Column("display_name", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=60), nullable=False),
        sa.Column("start_equity", sa.Numeric(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "arena_rounds",
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("winner", sa.String(length=10), nullable=True),
        sa.PrimaryKeyConstraint("number"),
    )
    op.create_table(
        "arena_rulebooks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trader", sa.String(length=10), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("rulebook", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("questions_version", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=60), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_arena_rulebooks_trader_round", "arena_rulebooks", ["trader", "round"])
    op.create_table(
        "arena_decisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trader", sa.String(length=10), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("candle_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("probabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("conviction", sa.Float(), nullable=True),
        sa.Column("min_conviction", sa.Float(), nullable=True),
        sa.Column("acted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_arena_decisions_trader_ts", "arena_decisions", ["trader", "candle_ts"])
    op.create_table(
        "arena_trades",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trader", sa.String(length=10), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("decision_id", sa.BigInteger(), nullable=True),
        sa.Column("side", sa.String(length=6), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Numeric(), nullable=False),
        sa.Column("entry_price", sa.Numeric(), nullable=False),
        sa.Column("exit_price", sa.Numeric(), nullable=True),
        sa.Column("tp_price", sa.Numeric(), nullable=False),
        sa.Column("sl_price", sa.Numeric(), nullable=False),
        sa.Column("tp_net_roe", sa.Numeric(), nullable=False),
        sa.Column("sl_net_roe", sa.Numeric(), nullable=False),
        sa.Column("margin_usdt", sa.Numeric(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fees_usdt", sa.Numeric(), nullable=True),
        sa.Column("pnl_usdt", sa.Numeric(), nullable=True),
        sa.Column("net_roe_pct", sa.Numeric(), nullable=True),
        sa.Column("exit_reason", sa.String(length=12), nullable=True),
        sa.Column("order_link_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_arena_trades_trader_opened", "arena_trades", ["trader", "opened_at"])
    op.create_table(
        "arena_bench_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trader", sa.String(length=10), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("loss_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "arena_equity",
        sa.Column("trader", sa.String(length=10), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity_usdt", sa.Numeric(), nullable=False),
        sa.Column("equity_index", sa.Numeric(), nullable=False),
        sa.PrimaryKeyConstraint("trader", "ts"),
    )


def downgrade() -> None:
    for name in (
        "arena_equity",
        "arena_bench_events",
        "arena_trades",
        "arena_decisions",
        "arena_rulebooks",
        "arena_rounds",
        "arena_traders",
    ):
        op.drop_table(name)
