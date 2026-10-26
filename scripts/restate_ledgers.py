"""Tie every paper ledger to its rows (2026-10-26): cash = starting capital + the net
result of every closed trade - the margin of every open one. Two faults had let the books
drift: the entry fee was deducted at the open and again inside the net at the close, and
the primary book carried a credit from 2026-10-15 that no row explains. The executor
recomputes equity on its next pass.

python -m scripts.restate_ledgers [--apply]
"""

from __future__ import annotations

import sys
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import PaperAccount, Position
from app.db.session import new_session


def main(argv: list[str] | None = None) -> int:
    apply = "--apply" in (argv or sys.argv[1:])
    with new_session() as s:
        for acct in s.scalars(select(PaperAccount).order_by(PaperAccount.id)).all():
            closed_net = s.scalar(
                select(func.coalesce(func.sum(Position.pnl), 0)).where(
                    Position.track == acct.track, Position.status == "closed"
                )
            ) or Decimal(0)
            open_margin = s.scalar(
                select(func.coalesce(func.sum(Position.margin), 0)).where(
                    Position.track == acct.track, Position.status.in_(["open", "closing"])
                )
            ) or Decimal(0)
            rows = s.scalar(select(func.count(Position.id)).where(Position.track == acct.track))
            if not rows:
                continue
            cash = acct.starting_capital + closed_net - open_margin
            equity = cash + open_margin  # the executor adds the unrealised on its next pass
            print(
                f"{acct.track}: cash {acct.cash:.2f} -> {cash:.2f} ({cash - acct.cash:+.2f}), "
                f"equity {acct.equity:.2f} -> about {equity:.2f}, realized {acct.realized_pnl:.2f} "
                f"vs rows {closed_net:.2f}"
            )
            if apply:
                acct.cash, acct.equity, acct.realized_pnl = cash, equity, closed_net
                if acct.goal_base is not None:
                    acct.goal_base = min(acct.goal_base, cash)
        if apply:
            s.commit()
            print("applied")
        else:
            print("dry run; add --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
