"""server_metrics: droplet usage from DigitalOcean monitoring

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-10 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "server_metrics",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_pct", sa.Float(), nullable=True),
        sa.Column("mem_used_pct", sa.Float(), nullable=True),
        sa.Column("mem_total", sa.BigInteger(), nullable=True),
        sa.Column("disk_used_pct", sa.Float(), nullable=True),
        sa.Column("disk_total", sa.BigInteger(), nullable=True),
        sa.Column("load_1", sa.Float(), nullable=True),
        sa.Column("load_5", sa.Float(), nullable=True),
        sa.Column("load_15", sa.Float(), nullable=True),
        sa.Column("net_in_mbps", sa.Float(), nullable=True),
        sa.Column("net_out_mbps", sa.Float(), nullable=True),
        sa.Column("info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("ts"),
    )


def downgrade() -> None:
    op.drop_table("server_metrics")
