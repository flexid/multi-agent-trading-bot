"""beta exposure per cycle

Revision ID: e3f5a7b9c1d2
Revises: d2e4f6a8b0c1
Create Date: 2026-10-10 05:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e3f5a7b9c1d2"
down_revision: str | None = "d2e4f6a8b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cycles", sa.Column("beta_exposure", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("cycles", "beta_exposure")
