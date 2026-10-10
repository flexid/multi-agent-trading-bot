"""Owner's terminal report: trades on the primary paper track, P&L and cost.

python -m app.report [HOURS]      # default 24; the `dorkbot` alias runs this over SSH
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.config import get_config
from app.db.models import LLMCall, PaperAccount, Position, SuppressedTrigger, XPostOut, XPostRecord
from app.db.session import new_session
from app.social.templates import holding_text

BRUSSELS = ZoneInfo("Europe/Brussels")
X_READ_USD = Decimal("0.005")
X_POST_USD = Decimal("0.015")


def fmt(x: Decimal | None) -> str:
    if x is None:
        return "-"
    return f"{x:.6g}" if x < 10 else f"{x:,.2f}".rstrip("0").rstrip(".")


def pct(x: Decimal | None) -> str:
    return "-" if x is None else f"{x * 100:+.2f}%"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    hours = int(args[0]) if args else 24
    cfg = get_config()
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with new_session() as s:
        rows = s.scalars(
            select(Position)
            .where(
                Position.track.in_(["primary", "live"]),
                (Position.status.in_(["open", "closing"])) | (Position.closed_at >= since),
            )
            .order_by(Position.opened_at)
        ).all()
        acct = s.get(PaperAccount, 1)
        llm_today = s.execute(
            select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(LLMCall.ts >= day)
        ).scalar_one()
        reads_today = s.execute(
            select(func.count()).select_from(XPostRecord).where(XPostRecord.fetched_at >= day)
        ).scalar_one()
        posts_today = s.execute(
            select(func.count())
            .select_from(XPostOut)
            .where(XPostOut.posted_at >= day, XPostOut.dry_run.is_(False))
        ).scalar_one()
        open_count = sum(1 for r in rows if r.status != "closed")
        suppressed = s.scalars(
            select(SuppressedTrigger)
            .where(SuppressedTrigger.ts >= since)
            .order_by(SuppressedTrigger.ts)
        ).all()

    stamp = f"{now.astimezone(BRUSSELS):%Y-%m-%d %H:%M}"
    print(f"dorkbot · {stamp} Brussels · primary paper track · last {hours}h")
    print(
        f"{'asset':8s} {'dir':5s} {'entry':>10s} {'exit':>10s} {'lev':>4s} {'stop':>10s} "
        f"{'target':>10s} {'price':>8s} {'margin':>8s}  {'held':10s} status"
    )
    for r in rows:
        held = ""
        if r.opened_at:
            end = r.closed_at or now
            held = holding_text((end - r.opened_at).total_seconds() / 3600)
        status = r.status if r.status != "closed" else f"closed:{r.close_reason}"
        print(
            f"{r.asset:8s} {r.direction:5s} {fmt(r.entry_price):>10s} {fmt(r.exit_price):>10s} "
            f"{r.leverage:>3.1f}x {fmt(r.stop):>10s} {fmt(r.target):>10s} "
            f"{pct(r.pnl_price_pct):>8s} {pct(r.pnl_margin_pct):>8s}  {held:10s} {status}"
        )
    if not rows:
        print("  no trades in the window")
    if acct:
        day_pnl = acct.equity - acct.day_start_equity
        total_pnl = acct.equity - acct.starting_capital
        day_pct = (day_pnl / acct.day_start_equity * 100) if acct.day_start_equity else 0
        total_pct = (total_pnl / acct.starting_capital * 100) if acct.starting_capital else 0
        print(
            f"\nequity {acct.equity:,.2f} {cfg.exchange.quote} · day P&L {day_pnl:+,.2f} "
            f"({day_pct:+.2f}%) · total P&L {total_pnl:+,.2f} ({total_pct:+.2f}%) · "
            f"{open_count} open"
        )
    if suppressed:
        print(f"\ntriggers suppressed by the 2/day cap · last {hours}h (logging only)")
        for st in suppressed:
            m4 = f"{st.move_4h_pct:+.2f}%" if st.move_4h_pct is not None else "pending"
            m1 = f"{st.move_1d_pct:+.2f}%" if st.move_1d_pct is not None else "pending"
            print(
                f"  {st.ts.astimezone(BRUSSELS):%m-%d %H:%M}  {st.reason:55.55s}  "
                f"spot {fmt(st.spot):>10s}  4h {m4:>8s}  1d {m1:>8s}"
            )
    x_cost = Decimal(reads_today) * X_READ_USD + Decimal(posts_today) * X_POST_USD
    print(
        f"cost today · LLM ${Decimal(str(llm_today)):.2f} · X ${x_cost:.2f} "
        f"({reads_today} reads, {posts_today} posts)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
