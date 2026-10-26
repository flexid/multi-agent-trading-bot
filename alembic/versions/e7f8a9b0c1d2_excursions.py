"""positions: the best and worst price excursion while open (MFE/MAE), for the exit review

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-10-26 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("mfe_pct", sa.Numeric(), nullable=True))
    op.add_column("positions", sa.Column("mae_pct", sa.Numeric(), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "mae_pct")
    op.drop_column("positions", "mfe_pct")
