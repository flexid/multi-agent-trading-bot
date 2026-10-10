"""logic version on cycles

Revision ID: f4a6b8c0d2e4
Revises: e3f5a7b9c1d2
Create Date: 2026-10-10 06:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4a6b8c0d2e4"
down_revision: str | None = "e3f5a7b9c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cycles", sa.Column("logic_version", sa.Integer(), nullable=True))
    op.execute("UPDATE cycles SET logic_version = 1 WHERE logic_version IS NULL")


def downgrade() -> None:
    op.drop_column("cycles", "logic_version")
