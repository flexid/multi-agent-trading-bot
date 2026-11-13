"""the slow books: rule plans, and the book and plan on a position

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-11-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a9b0c1d2e3f4"
down_revision: str | None = "f8a9b0c1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rule_plans",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("book", sa.String(length=12), nullable=False),
        sa.Column("track", sa.String(length=8), nullable=False),
        sa.Column("asset", sa.String(length=10), nullable=False),
        sa.Column("side", sa.String(length=6), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("weight", sa.Numeric(), nullable=True),
        sa.Column("plan", postgresql.JSONB(), nullable=True),
        sa.Column("basis", postgresql.JSONB(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("exec_notes", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint("book", "asset", "day", name="uq_rule_plans_book_asset_day"),
    )
    op.create_index("ix_rule_plans_book_asset_ts", "rule_plans", ["book", "asset", "ts"])
    op.add_column("positions", sa.Column("book", sa.String(length=12), nullable=True))
    op.add_column("positions", sa.Column("plan_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "plan_id")
    op.drop_column("positions", "book")
    op.drop_index("ix_rule_plans_book_asset_ts", table_name="rule_plans")
    op.drop_table("rule_plans")
