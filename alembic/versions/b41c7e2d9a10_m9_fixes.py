"""m9 fixes: backup stop, exit progress, capital ramp

Revision ID: b41c7e2d9a10
Revises: 9050f51212a2
Create Date: 2026-10-09 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b41c7e2d9a10"
down_revision: str | None = "9050f51212a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("atr", sa.Numeric(precision=28, scale=10), nullable=True))
    op.add_column(
        "positions",
        sa.Column("backup_stop_price", sa.Numeric(precision=28, scale=10), nullable=True),
    )
    op.add_column("positions", sa.Column("backup_stop_link", sa.String(length=36), nullable=True))
    op.add_column(
        "positions",
        sa.Column("exit_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    for name in ("exit_filled_qty", "exit_value", "exit_fee"):
        op.add_column(
            "positions",
            sa.Column(name, sa.Numeric(precision=28, scale=10), server_default="0", nullable=False),
        )
    op.add_column(
        "risk_state", sa.Column("capital_fraction", sa.Numeric(precision=4, scale=2), nullable=True)
    )
    op.add_column(
        "risk_state", sa.Column("capital_step_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("risk_state", "capital_step_at")
    op.drop_column("risk_state", "capital_fraction")
    for name in ("exit_fee", "exit_value", "exit_filled_qty"):
        op.drop_column("positions", name)
    op.drop_column("positions", "exit_attempts")
    op.drop_column("positions", "backup_stop_link")
    op.drop_column("positions", "backup_stop_price")
    op.drop_column("positions", "atr")
