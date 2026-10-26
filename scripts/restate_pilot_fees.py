"""Restate the pilot's fees once (2026-10-26): Bybit charges a spot buy's fee in the coin
bought and the gateway booked it as quote. A long's entry fee and a short's exit fee were
therefore in base units; this converts them at the fill price, recomputes the trade's net
result and moves the difference through the pilot ledger. Idempotent: a row is restated
only when its fee exceeds half a percent of its notional, which no real fee does.

python -m scripts.restate_pilot_fees [--apply]
"""

from __future__ import annotations

import sys
from decimal import Decimal

from sqlalchemy import select

from app.db.models import PaperAccount, Position
from app.db.session import new_session

SUSPECT = Decimal("0.005")  # fees above this share of the notional are in the wrong unit


def main(argv: list[str] | None = None) -> int:
    apply = "--apply" in (argv or sys.argv[1:])
    delta_total = Decimal(0)
    with new_session() as s:
        rows = s.scalars(select(Position).where(Position.mode == "pilot")).all()
        for r in rows:
            if not r.notional or r.fees <= r.notional * SUSPECT:
                continue
            entry_fee = r.fees - (r.exit_fee or 0)
            new_entry_fee, new_exit_fee = entry_fee, r.exit_fee or Decimal(0)
            if r.direction == "long" and r.entry_price and entry_fee > r.notional * SUSPECT:
                new_entry_fee = entry_fee * r.entry_price  # base coin -> quote
            if r.direction == "short" and r.exit_price and new_exit_fee > r.notional * SUSPECT:
                new_exit_fee = new_exit_fee * r.exit_price
            new_fees = new_entry_fee + new_exit_fee
            delta = r.fees - new_fees  # what was overcharged
            print(
                f"{r.id} {r.asset} {r.direction} {r.status}: fees {r.fees:.4f} -> {new_fees:.4f}"
                + (f", pnl {r.pnl:.4f} -> {(r.pnl + delta):.4f}" if r.pnl is not None else "")
            )
            if apply:
                r.fees, r.exit_fee = new_fees, new_exit_fee
                if r.status == "closed" and r.pnl is not None:
                    r.pnl = r.pnl + delta
                    r.pnl_margin_pct = r.pnl / r.margin if r.margin else r.pnl_margin_pct
                    delta_total += delta
        acct = s.scalar(select(PaperAccount).where(PaperAccount.track == "pilot"))
        if acct is not None and delta_total:
            print(
                f"pilot ledger: cash {acct.cash:.2f} -> {(acct.cash + delta_total):.2f}, "
                f"fees_paid {acct.fees_paid:.2f} -> {(acct.fees_paid - delta_total):.2f}"
            )
            if apply:
                acct.cash += delta_total
                acct.equity += delta_total
                acct.realized_pnl += delta_total
                acct.fees_paid -= delta_total
        if apply:
            s.commit()
            print("applied")
        else:
            print("dry run; add --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
