"""The slow books' daily rule cycle (docs/RETHINK_2026-11.md, owner 2026-11-12).

Once a day, after the daily candle has closed, this module decides what each slow book
wants to hold in each asset and writes the executor's instruction: one ``rule_plans`` row
per book, asset and day. No model is called here.

- ``trend`` (the champion) wants a long when the majority of the three trend votes is
  long, otherwise cash. ``short`` wants a short when the majority is short, otherwise cash.
- ``challenger`` wants what the three managers called in the day's first finished full
  cycle (their two-of-three consensus, before the fast book's risk rules), traded with the
  champion's structure, so the two books differ only in who decides the direction.

Every position is one whole position per asset, sized at the entry: the ledger's equity
divided by the number of assets, times ``min(1, vol_target / volatility)``. No leverage,
no take-profit; the exit is the signal. One disaster stop, ``disaster_stop`` from the
entry, on touch. A position closed by a stop or a news shock sits out until its signal has
turned off and on again. Missing data means no row and no new position.

The executor acts on the rows (app/execution/executor.py): it opens on a fresh row with
``action == "open"`` when nothing is open for that book and asset, and closes a position
whose side the latest row no longer wants.

python -m app.risk.rulebook        run the cycle now (idempotent: a row exists once a day)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Config, Secrets, get_config, get_secrets
from app.db.models import Candle, Cycle, DecisionRecord, PaperAccount, Position, RulePlan
from app.db.session import new_session
from app.decision.evidence import spot_and_atr
from app.strategy import trend
from app.strategy.books import book_tracks

log = logging.getLogger("rulebook")

FAR = Decimal(1000)  # a target that is never meant to be reached: the signal is the exit
MAX_HOLD_HOURS = 43_800  # five years: no time stop
HISTORY_DAYS = 120  # daily closes loaded per asset; the longest vote needs 57
CENT = Decimal("0.01")
# A position these reasons closed sits out until its signal has turned off and on again.
# A kill switch or the brake is the owner's call: after a resume the book may re-enter.
SIT_OUT_REASONS = frozenset({"stop", "stop_hard", "shock", "liquidation"})
OPEN_STATES = ("open", "closing")


def day_start(now: datetime) -> datetime:
    """00:00 UTC of the day ``now`` falls in."""
    now = now.astimezone(UTC)
    return datetime(now.year, now.month, now.day, tzinfo=UTC)


def daily_closes(
    session: Session, symbol: str, before: datetime
) -> tuple[list[float], date | None]:
    """The closed daily closes of ``symbol`` that opened before ``before``, oldest first,
    and the day of the last one. Only closed candles are ever stored (app/data/fetch.py)."""
    rows = session.execute(
        select(Candle.open_time, Candle.close)
        .where(Candle.symbol == symbol, Candle.interval == "D", Candle.open_time < before)
        .order_by(Candle.open_time.desc())
        .limit(HISTORY_DAYS)
    ).all()
    if not rows:
        return [], None
    rows = list(reversed(rows))
    return [float(c) for _, c in rows], rows[-1][0].astimezone(UTC).date()


def missing_candles(session: Session, cfg: Config, now: datetime) -> list[str]:
    """The assets whose last closed daily candle is not yesterday's."""
    start = day_start(now)
    want = (start - timedelta(days=1)).date()
    out = []
    for asset in cfg.trading.assets:
        _, last = daily_closes(session, cfg.symbol(asset), start)
        if last != want:
            out.append(asset)
    return out


def managers_call(session: Session, now: datetime) -> tuple[int, dict[str, DecisionRecord]] | None:
    """The day's first finished full managers' cycle and its decision per asset: the one
    daily call the challenger trades. None while no such cycle has finished today."""
    cycle = session.scalars(
        select(Cycle)
        .where(Cycle.kind != "light", Cycle.status == "done", Cycle.started_at >= day_start(now))
        .order_by(Cycle.started_at)
        .limit(1)
    ).first()
    if cycle is None:
        return None
    rows = session.scalars(select(DecisionRecord).where(DecisionRecord.cycle_id == cycle.id)).all()
    return cycle.id, {d.asset: d for d in rows}


def equity_of(session: Session, cfg: Config, track: str) -> Decimal:
    """The ledger's equity, or the paper capital before the executor has created it."""
    acct = session.scalars(select(PaperAccount).where(PaperAccount.track == track)).first()
    if acct is not None and acct.equity > 0:
        return Decimal(acct.equity)
    return Decimal(cfg.trading.capital_max_usdt)


def sits_out(session: Session, book: str, asset: str, side: str) -> bool:
    """True when the last position of this book and asset was closed on this side by a
    stop or a shock and no daily row since then has wanted anything else."""
    last = session.scalars(
        select(Position)
        .where(Position.book == book, Position.asset == asset, Position.status == "closed")
        .order_by(Position.closed_at.desc())
        .limit(1)
    ).first()
    if last is None or last.closed_at is None:
        return False
    if last.close_reason not in SIT_OUT_REASONS or last.direction != side:
        return False
    reset = session.scalars(
        select(RulePlan.id)
        .where(
            RulePlan.book == book,
            RulePlan.asset == asset,
            RulePlan.ts > last.closed_at,
            RulePlan.side != side,
        )
        .limit(1)
    ).first()
    return reset is None


def plan_for(
    cfg: Config,
    side: str,
    close: float,
    weight: float,
    equity: Decimal,
    atr: float | None = None,
) -> dict[str, Any] | None:
    """The executor's instruction for one whole position, or None when the size is below
    the minimum. One slot is the ledger's equity over the number of assets; the weight is
    never above 1, so the book never borrows to be long and never holds more than its
    equity."""
    st = cfg.strategy
    slot = equity / max(1, len(cfg.trading.assets))
    notional = (slot * Decimal(str(min(1.0, max(0.0, weight))))).quantize(CENT)
    if notional < st.min_notional_usdt:
        return None
    entry = Decimal(str(close))
    long = side == "long"
    stop = entry * (1 - st.disaster_stop) if long else entry * (1 + st.disaster_stop)
    target = entry * FAR if long else entry / FAR
    plan: dict[str, Any] = {
        "side": side,
        "entry": f"{entry:f}",
        "stop": f"{stop:f}",
        "hard_stop": f"{stop:f}",
        "target": f"{target:f}",
        "max_hold_hours": MAX_HOLD_HOURS,
        "leverage": "1",
        "borrow": not long,
        "notional": f"{notional:f}",
        "margin": f"{notional:f}",
    }
    if atr:
        # 14 x 4h, as on the fast plans: the news-shock exit asks for a move of one ATR
        plan["atr"] = f"{atr:.10g}"
    return plan


def _row(
    session: Session,
    cfg: Config,
    *,
    now: datetime,
    day: date,
    book: str,
    track: str,
    asset: str,
    side: str,
    weight: float,
    close: float,
    basis: dict[str, Any],
    atr: float | None = None,
) -> RulePlan:
    held = session.scalars(
        select(Position).where(
            Position.track == track,
            Position.book == book,
            Position.asset == asset,
            Position.status.in_(OPEN_STATES),
        )
    ).first()
    plan: dict[str, Any] | None = None
    note: str | None = None
    if held is not None and held.direction == side:
        action = "hold"
    elif side == "flat":
        action = "close" if held is not None else "none"
    elif held is None and sits_out(session, book, asset, side):
        action, note = (
            "blocked",
            "stopped out of this signal; back in when it has turned off and on",
        )
    else:
        plan = plan_for(cfg, side, close, weight, equity_of(session, cfg, track), atr)
        if plan is None:
            action, note = "none", "the size is below the minimum order"
            if held is not None:  # the other side is still open: it must go either way
                action, note = (
                    "close",
                    "the other side is wanted, but its size is below the minimum",
                )
        else:
            action = "open"
            if held is not None:
                note = f"flip: the open {held.direction} closes first"
    return RulePlan(
        ts=now,
        day=day,
        book=book,
        track=track,
        asset=asset,
        side=side,
        action=action,
        weight=Decimal(str(round(weight, 6))),
        plan=plan,
        basis=basis,
        note=note,
    )


def decide(session: Session, cfg: Config, now: datetime) -> list[RulePlan]:
    """Write today's rows for every book and asset that has none yet. Returns the new rows."""
    books = book_tracks(cfg)
    if not books:
        return []
    st = cfg.strategy
    start = day_start(now)
    day = (start - timedelta(days=1)).date()
    call = managers_call(session, now) if "challenger" in books else None
    existing = {
        (b, a)
        for b, a in session.execute(
            select(RulePlan.book, RulePlan.asset).where(RulePlan.day == day)
        ).all()
    }
    out: list[RulePlan] = []
    for asset in cfg.trading.assets:
        closes, last = daily_closes(session, cfg.symbol(asset), start)
        if last != day:
            log.warning(
                "rule cycle: %s has no closed daily candle for %s (last: %s)", asset, day, last
            )
            continue
        sig = trend.signal(
            closes,
            lookbacks=st.lookbacks,
            vol_target=float(st.vol_target),
            vol_window=st.vol_window_days,
        )
        if sig is None:
            log.warning("rule cycle: %s has too little daily history for a signal", asset)
            continue
        _, atr = spot_and_atr(session, cfg.symbol(asset))
        signal_basis = {
            "votes": list(sig.votes),
            "lookbacks": list(st.lookbacks),
            "signal": sig.side,
            "vol": round(sig.vol, 4) if sig.vol else None,
            "close": sig.close,
        }
        for book, track in books.items():
            if (book, asset) in existing:
                continue
            if book == "challenger":
                d = call[1].get(asset) if call is not None else None
                if call is None or d is None:
                    continue  # no finished managers' cycle yet today: a later run writes it
                side = d.direction if d.direction in ("long", "short") else "flat"
                basis: dict[str, Any] = {
                    "cycle_id": call[0],
                    "decision_id": d.id,
                    "seats": [d.pm1_direction, d.pm2_direction, d.pm3_direction],
                    "agreement": d.agreement,
                    "conviction": d.conviction,
                    "vol": signal_basis["vol"],
                    "close": sig.close,
                }
            else:
                want = "long" if book == "trend" else "short"
                side = want if sig.side == want else "flat"
                basis = signal_basis
            row = _row(
                session,
                cfg,
                now=now,
                day=day,
                book=book,
                track=track,
                asset=asset,
                side=side,
                weight=sig.weight,
                close=sig.close,
                basis=basis,
                atr=atr,
            )
            session.add(row)
            out.append(row)
    # An asset that left the universe has no signal any more: its slow positions close.
    written = existing | {(r.book, r.asset) for r in out}
    orphans = session.scalars(
        select(Position).where(
            Position.book.in_(list(books)),
            Position.status.in_(OPEN_STATES),
            Position.asset.not_in(cfg.trading.assets),
        )
    ).all()
    for held in orphans:
        if held.book is None or (held.book, held.asset) in written:
            continue
        written.add((held.book, held.asset))
        row = RulePlan(
            ts=now,
            day=day,
            book=held.book,
            track=held.track,
            asset=held.asset,
            side="flat",
            action="close",
            note="the asset left the universe",
        )
        session.add(row)
        out.append(row)
    try:
        session.commit()
    except IntegrityError:  # another run wrote the same day at the same moment
        session.rollback()
        log.info("rule cycle: the plans for %s were written by another run", day)
        return []
    return out


async def run(
    cfg: Config | None = None, secrets: Secrets | None = None, now: datetime | None = None
) -> list[RulePlan]:
    """The daily job: make sure yesterday's candle is there, then decide."""
    cfg = cfg or get_config()
    if not book_tracks(cfg):
        return []
    now = now or datetime.now(UTC)
    with new_session() as session:
        missing = missing_candles(session, cfg, now)
    if missing:
        from app.data import fetch

        log.info("rule cycle: fetching candles, %s lack yesterday's close", ", ".join(missing))
        await fetch.run_job(
            "bybit.candles", fetch.make_fetch_candles(cfg, secrets or get_secrets())
        )
    with new_session() as session:
        rows = decide(session, cfg, now)
        summary = [f"{r.book} {r.asset} {r.side} ({r.action})" for r in rows if r.action != "none"]
    if rows:
        log.info(
            "rule cycle: %d rows written; %s", len(rows), "; ".join(summary) or "nothing to do"
        )
    return rows


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    rows = asyncio.run(run())
    with new_session() as session:
        day = (day_start(datetime.now(UTC)) - timedelta(days=1)).date()
        for r in session.scalars(
            select(RulePlan).where(RulePlan.day == day).order_by(RulePlan.book, RulePlan.asset)
        ).all():
            size = (r.plan or {}).get("notional", "")
            print(f"{r.book:10} {r.asset:5} {r.side:5} {r.action:8} {size:>10} {r.note or ''}")
    print(f"{len(rows)} new row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
