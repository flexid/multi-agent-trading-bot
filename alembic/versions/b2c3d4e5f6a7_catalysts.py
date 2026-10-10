"""catalysts: catalyst_events and x_post_labels (Jev for the alt sleeve)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-10 20:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalyst_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("source_id", sa.String(length=120), nullable=False),
        sa.Column("author", sa.String(length=64), nullable=True),
        sa.Column("url", sa.String(length=300), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hint_asset", sa.String(length=10), nullable=True),
        sa.Column("asset", sa.String(length=10), nullable=True),
        sa.Column("type", sa.String(length=12), nullable=True),
        sa.Column("direction", sa.String(length=8), nullable=True),
        sa.Column("materiality", sa.Numeric(4, 3), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("supply_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("classifier", sa.String(length=40), nullable=True),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("shadow", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_id", name="uq_catalyst_source"),
    )
    op.create_index("ix_catalyst_asset_event_at", "catalyst_events", ["asset", "event_at"])
    op.create_table(
        "x_post_labels",
        sa.Column("post_id", sa.String(length=32), nullable=False),
        sa.Column("labeler", sa.String(length=10), nullable=False),
        sa.Column("model", sa.String(length=60), nullable=False),
        sa.Column("asset", sa.String(length=10), nullable=True),
        sa.Column("stance", sa.String(length=10), nullable=True),
        sa.Column("kind", sa.String(length=10), nullable=True),
        sa.Column("credibility", sa.Numeric(4, 3), nullable=True),
        sa.Column("shock", sa.Boolean(), nullable=True),
        sa.Column("labeled_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["post_id"], ["x_posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("post_id", "labeler"),
    )


def downgrade() -> None:
    op.drop_table("x_post_labels")
    op.drop_index("ix_catalyst_asset_event_at", table_name="catalyst_events")
    op.drop_table("catalyst_events")
