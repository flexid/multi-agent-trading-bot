"""dex: a mode on every row, so paper, testnet and live keep separate records

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-10-20 03:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: str | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dex_trades", sa.Column("mode", sa.String(length=8), nullable=False, server_default="paper"))
    op.add_column("dex_equity", sa.Column("mode", sa.String(length=8), nullable=False, server_default="paper"))
    op.add_column("dex_state", sa.Column("mode", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("dex_state", "mode")
    op.drop_column("dex_equity", "mode")
    op.drop_column("dex_trades", "mode")
