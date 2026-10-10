"""sleeves: position sleeve, sleeve modes and day locks

Revision ID: a1b2c3d4e5f6
Revises: f4a6b8c0d2e4
Create Date: 2026-10-10 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f4a6b8c0d2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("sleeve", sa.String(length=16), nullable=True))
    op.add_column(
        "risk_state",
        sa.Column("sleeve_modes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "risk_state",
        sa.Column("day_locked_sleeves", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("risk_state", "day_locked_sleeves")
    op.drop_column("risk_state", "sleeve_modes")
    op.drop_column("positions", "sleeve")
