"""suppressed triggers log

Revision ID: d2e4f6a8b0c1
Revises: c7d1e5f2a9b3
Create Date: 2026-10-10 04:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d2e4f6a8b0c1"
down_revision: str | None = "c7d1e5f2a9b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "suppressed_triggers",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("asset", sa.String(length=10), nullable=True),
        sa.Column("spot", sa.Numeric(28, 10), nullable=True),
        sa.Column("move_4h_pct", sa.Float(), nullable=True),
        sa.Column("move_1d_pct", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("suppressed_triggers")
