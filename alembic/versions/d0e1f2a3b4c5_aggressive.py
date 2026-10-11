"""aggressive mode: risk_state.aggressive(+since), cycles.aggressive

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-10-11 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "risk_state",
        sa.Column("aggressive", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "risk_state", sa.Column("aggressive_since", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("cycles", sa.Column("aggressive", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("cycles", "aggressive")
    op.drop_column("risk_state", "aggressive_since")
    op.drop_column("risk_state", "aggressive")
