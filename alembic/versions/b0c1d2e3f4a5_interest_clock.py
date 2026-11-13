"""a position's interest clock: the hour up to which its borrow interest is booked

Until now the executor measured the hours since the row was last touched, and it touches
every open row on every pass, so no whole hour ever passed and no interest was booked. A
row without a clock (opened before this revision) counts from its opening.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-11-12 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b0c1d2e3f4a5"
down_revision: str | None = "a9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("interest_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "interest_at")
