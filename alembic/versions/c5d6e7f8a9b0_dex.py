"""the DEX mirror's ledger: trades, equity, state

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-10-19 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dex_trades",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("asset", sa.String(length=10), nullable=False),
        sa.Column("side", sa.String(length=6), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Numeric(), nullable=False),
        sa.Column("entry_price", sa.Numeric(), nullable=False),
        sa.Column("exit_price", sa.Numeric(), nullable=True),
        sa.Column("tp_price", sa.Numeric(), nullable=False),
        sa.Column("sl_price", sa.Numeric(), nullable=False),
        sa.Column("margin_usdt", sa.Numeric(), nullable=False),
        sa.Column("main_position_id", sa.BigInteger(), nullable=False),
        sa.Column("main_entry", sa.Numeric(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fees_usdt", sa.Numeric(), nullable=True),
        sa.Column("pnl_usdt", sa.Numeric(), nullable=True),
        sa.Column("net_roe_pct", sa.Numeric(), nullable=True),
        sa.Column("exit_reason", sa.String(length=24), nullable=True),
        sa.Column("tx", sa.String(length=80), nullable=True),
        sa.Column("cloid", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dex_trades_status", "dex_trades", ["status"])
    op.create_table(
        "dex_equity",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity_usdt", sa.Numeric(), nullable=False),
        sa.Column("equity_index", sa.Numeric(), nullable=False),
        sa.PrimaryKeyConstraint("ts"),
    )
    op.create_table(
        "dex_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("start_equity", sa.Numeric(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fill_check", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mismatch", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("dex_state")
    op.drop_table("dex_equity")
    op.drop_index("ix_dex_trades_status", table_name="dex_trades")
    op.drop_table("dex_trades")
