"""x_posts.fresh and x_post_labels.fresh: new information or a recap (logged only)

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-10-11 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("x_posts", sa.Column("fresh", sa.Boolean(), nullable=True))
    op.add_column("x_post_labels", sa.Column("fresh", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("x_post_labels", "fresh")
    op.drop_column("x_posts", "fresh")
