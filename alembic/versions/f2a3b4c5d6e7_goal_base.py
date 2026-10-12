"""paper_account.goal_base: the equity aggressive mode's next day goal is measured from

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-12 02:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("paper_account", sa.Column("goal_base", sa.Numeric(), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_account", "goal_base")
