"""positions.profit_peak_pct: the trailing take-profit's high-water mark

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-10-12 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("profit_peak_pct", sa.Numeric(), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "profit_peak_pct")
