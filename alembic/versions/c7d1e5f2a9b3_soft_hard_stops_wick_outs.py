"""soft and hard stops, wick-outs, stop multiples

Revision ID: c7d1e5f2a9b3
Revises: b41c7e2d9a10
Create Date: 2026-10-10 03:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7d1e5f2a9b3"
down_revision: str | None = "b41c7e2d9a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("hard_stop", sa.Numeric(28, 10), nullable=True))
    op.add_column("positions", sa.Column("wick_out", sa.Boolean(), nullable=True))
    op.add_column("risk_state", sa.Column("stop_buffer_atr", sa.Numeric(3, 2), nullable=True))
    op.add_column("risk_state", sa.Column("hard_stop_atr", sa.Numeric(3, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("risk_state", "hard_stop_atr")
    op.drop_column("risk_state", "stop_buffer_atr")
    op.drop_column("positions", "wick_out")
    op.drop_column("positions", "hard_stop")
