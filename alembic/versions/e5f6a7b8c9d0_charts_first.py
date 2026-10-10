"""decisions.charts_first_score: the logged charts-first shadow of the formula score

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-11 00:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("decisions", sa.Column("charts_first_score", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("decisions", "charts_first_score")
