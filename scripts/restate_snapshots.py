"""Rewrite the primary book's equity history to where the book really was (owner
2026-10-26): the credit of about 290 that appeared in the pass of 2026-10-15 02:36 UTC
and that no row explains is taken out of every primary snapshot from that moment on. The
ledger itself was corrected on 2026-10-26 16:30 UTC; without this the chart shows that
correction as a cliff on the 26th instead of a book that was always 290 lower.

python -m scripts.restate_snapshots [--apply]
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, update

from app.db.models import EquitySnapshot
from app.db.session import new_session

MODE = "paper:primary"
SINCE = datetime(2026, 10, 15, 2, 36, 0, 736212, tzinfo=UTC)  # the pass with the credit
UNTIL = datetime(2026, 10, 26, 16, 29, 20, tzinfo=UTC)  # the ledger restatement took it out
CREDIT = Decimal("277.56")  # the jump that pass recorded beyond its closes' net result


def main(argv: list[str] | None = None) -> int:
    apply = "--apply" in (argv or sys.argv[1:])
    with new_session() as s:
        where = (
            EquitySnapshot.mode == MODE,
            EquitySnapshot.ts >= SINCE,
            EquitySnapshot.ts < UNTIL,
        )
        n = s.scalar(select(func.count()).where(*where)) or 0
        first = s.scalar(select(func.min(EquitySnapshot.ts)).where(*where))
        last = s.scalar(select(func.max(EquitySnapshot.ts)).where(*where))
        before = s.scalar(
            select(EquitySnapshot.equity)
            .where(EquitySnapshot.mode == MODE, EquitySnapshot.ts < SINCE)
            .order_by(EquitySnapshot.ts.desc())
            .limit(1)
        )
        at = s.scalar(
            select(EquitySnapshot.equity).where(
                EquitySnapshot.mode == MODE, EquitySnapshot.ts == SINCE
            )
        )
        print(f"{n} primary snapshots from {first} to {last} go down by {CREDIT}")
        after = at - CREDIT if at is not None else None
        print(f"the pass itself: {before} before -> {at} recorded -> {after} after")
        if apply:
            s.execute(
                update(EquitySnapshot)
                .where(*where)
                .values(equity=EquitySnapshot.equity - CREDIT, cash=EquitySnapshot.cash - CREDIT)
            )
            s.commit()
            print("applied")
        else:
            print("dry run; add --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
