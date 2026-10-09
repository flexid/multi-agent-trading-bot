"""Owner's pre-shadow checklist: partial fill, rejected order, feed drop during an active
stop, restart mid-trade, failed borrow, gap through a stop, exchange unreachable.

These run against the local Postgres (skipped when it is down) with injected quotes and
a scripted gateway; no network, no real orders.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import delete, select

from app.config import get_config, get_secrets
from app.db.models import ControlRequest, Cycle, DecisionRecord, PaperAccount, Position, XPostOut
from app.execution.executor import Executor
from app.execution.gateway import OrderOutcome, Outcome, ScriptedGateway
from app.execution.simulator import Quote
from tests.test_data_layer import needs_db

NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(
    minutes=5
)  # the executor only opens decisions younger than one cycle
ASSET = "BTC"


def quote(bid: str, ask: str, when: datetime = NOW) -> Quote:
    return Quote(D(bid), D(ask), when)


@pytest.fixture
def db() -> Iterator[None]:
    """Clean slate: no positions, decisions or paper ledgers from other tests or cycles."""
    from app.db.session import new_session

    with new_session() as s:
        s.execute(delete(XPostOut))
        s.execute(delete(ControlRequest))
        s.execute(delete(Position))
        s.execute(delete(DecisionRecord))
        s.execute(delete(Cycle))
        s.execute(delete(PaperAccount))
        s.commit()
    yield
    with new_session() as s:
        s.execute(delete(XPostOut))
        s.execute(delete(ControlRequest))
        s.execute(delete(Position))
        s.execute(delete(DecisionRecord))
        s.execute(delete(Cycle))
        s.execute(delete(PaperAccount))
        s.commit()


def seed_decision(
    direction: str = "long",
    entry: str = "100",
    stop: str = "98",
    target: str = "106",
    atr: str | None = None,
) -> int:
    from app.db.session import new_session

    with new_session() as s:
        cycle = Cycle(started_at=NOW, kind="scheduled", mode="shadow", status="done")
        s.add(cycle)
        s.commit()
        d = DecisionRecord(
            cycle_id=cycle.id,
            asset=ASSET,
            ts=NOW,
            spot=D(entry),
            valid_agents=5,
            formula_score=0.3,
            agreement="agree",
            consensus_score=0.4,
            direction=direction,
            conviction=0.7,
            reason="PMs agree",
            action="open",
            proposal={
                "plan": {
                    "entry": entry,
                    "stop": stop,
                    "target": target,
                    "max_hold_hours": 24,
                    "leverage": "2.0",
                    "borrow": True,
                    "notional": "2000",
                    "margin": "1000",
                    **({"atr": atr} if atr else {}),
                },
                "plan_max": {
                    "entry": entry,
                    "stop": stop,
                    "target": target,
                    "max_hold_hours": 24,
                    "leverage": "5.0",
                    "borrow": True,
                    "notional": "5000",
                    "margin": "1000",
                },
            },
        )
        s.add(d)
        s.commit()
        return d.id


def make(gateway: ScriptedGateway, q: Quote | None) -> Executor:
    ex = Executor(get_config(), get_secrets(), gateway, feed=False)
    ex.posting_live = lambda: False  # type: ignore[method-assign]  # never post from tests
    symbol = get_config().symbol(ASSET)
    if q is not None:
        ex.quotes[symbol] = q
    ex.qty_steps[symbol] = D("0.001")
    return ex


def positions(track: str = "primary") -> list[Position]:
    from app.db.session import new_session

    with new_session() as s:
        return list(
            s.scalars(select(Position).where(Position.track == track).order_by(Position.id)).all()
        )


@needs_db
async def test_partial_fill_opens_the_filled_quantity_only(db: None) -> None:
    seed_decision()
    gw = ScriptedGateway(opens=[OrderOutcome(Outcome.PARTIAL, filled_qty=D("12"), fee=D("1.2"))])
    await make(gw, quote("99.9", "100")).tick()
    (pos,) = positions()
    assert pos.qty == D("12") and pos.status == "open"
    assert pos.margin == pytest.approx(D(600), abs=D("0.01"))  # 12 of 20 units at 2x


@needs_db
async def test_rejected_order_marks_the_decision_and_opens_nothing(db: None) -> None:
    did = seed_decision()
    gw = ScriptedGateway(opens=[OrderOutcome(Outcome.REJECTED, detail="insufficient balance")])
    await make(gw, quote("99.9", "100")).tick()
    assert positions() == []
    from app.db.session import new_session

    with new_session() as s:
        assert s.get(DecisionRecord, did).action == "rejected"  # type: ignore[union-attr]


@needs_db
async def test_failed_borrow_blocks_the_entry(db: None) -> None:
    seed_decision(direction="short", stop="102", target="94")
    gw = ScriptedGateway(opens=[OrderOutcome(Outcome.BORROW_FAILED, detail="no borrow available")])
    await make(gw, quote("99.9", "100")).tick()
    assert positions() == []


@needs_db
async def test_feed_drop_keeps_the_stop_armed_and_fires_on_the_next_quote(db: None) -> None:
    seed_decision()
    gw = ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)])
    ex = make(gw, quote("99.9", "100"))
    await ex.tick()
    assert positions()[0].status == "open"
    ex.quotes_stale = True  # the feed is gone
    await ex.tick()
    assert positions()[0].status == "open"  # nothing evaluated, nothing closed, no crash
    ex.quotes_stale = False
    ex.quotes[get_config().symbol(ASSET)] = quote("97.5", "97.6")  # back, below the stop
    await ex.tick()
    (pos,) = positions()
    assert pos.status == "closed" and pos.close_reason == "stop"


@needs_db
async def test_restart_mid_trade_reloads_positions_and_enforces_stops(db: None) -> None:
    seed_decision()
    await make(ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)]), quote("99.9", "100")).tick()
    assert positions()[0].status == "open"
    fresh = make(ScriptedGateway(opens=[]), quote("97", "97.1"))  # a new process, nothing in memory
    await fresh.tick()
    (pos,) = positions()
    assert pos.status == "closed" and pos.close_reason == "stop"


@needs_db
async def test_gap_through_stop_fills_at_the_market_not_at_the_stop(db: None) -> None:
    seed_decision(stop="98")
    ex = make(ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)]), quote("99.9", "100"))
    await ex.tick()
    ex.quotes[get_config().symbol(ASSET)] = quote("90", "90.1")  # gapped 8% through the stop
    await ex.tick()
    (pos,) = positions()
    assert pos.close_reason == "stop"
    assert pos.exit_price == D(90) and pos.exit_price < pos.stop
    assert pos.pnl is not None and pos.pnl < -D(190)  # the loss is the real one, not 2%


@needs_db
async def test_exchange_unreachable_retries_the_exit_until_it_lands(db: None) -> None:
    seed_decision()
    # Both shadow tracks open; on the exit tick the primary's close is unreachable, the
    # comparison track's lands; the primary retries on the next tick and then closes.
    gw = ScriptedGateway(
        opens=[OrderOutcome(Outcome.FILLED)],
        closes=[OrderOutcome(Outcome.UNREACHABLE), OrderOutcome(Outcome.FILLED)],
    )
    ex = make(gw, quote("99.9", "100"))
    await ex.tick()
    ex.quotes[get_config().symbol(ASSET)] = quote("97", "97.1")
    await ex.tick()
    (pos,) = positions()
    assert pos.status == "closing" and pos.close_reason == "stop"  # armed, venue down
    assert positions("max")[0].status == "closed"
    await ex.tick()
    (pos,) = positions()
    assert pos.status == "closed" and pos.close_reason == "stop"
    assert [c[0] for c in gw.calls] == ["open", "open", "close", "close", "close"]


@needs_db
async def test_unreachable_entry_is_retried_next_tick_not_duplicated(db: None) -> None:
    seed_decision()
    gw = ScriptedGateway(opens=[OrderOutcome(Outcome.UNREACHABLE), OrderOutcome(Outcome.FILLED)])
    ex = make(gw, quote("99.9", "100"))
    await ex.tick()
    assert positions() == []
    await ex.tick()
    assert len(positions()) == 1


@needs_db
async def test_close_post_is_queued_with_the_booked_pnl(db: None) -> None:
    """The close post renders from the P&L fields, so it must be queued after they are set
    (before this fix every close post failed inside the never-block-trading catch)."""
    from app.db.session import new_session

    seed_decision(stop="98")
    ex = make(ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)]), quote("99.9", "100"))
    await ex.tick()
    ex.quotes[get_config().symbol(ASSET)] = quote("97", "97.1")
    await ex.tick()
    (pos,) = positions()
    assert pos.status == "closed"
    with new_session() as s:
        kinds = (
            s.execute(
                select(XPostOut.kind).where(XPostOut.position_id == pos.id).order_by(XPostOut.id)
            )
            .scalars()
            .all()
        )
    assert kinds == ["open", "close"]


def _kill(source: str) -> None:
    from app.db.session import new_session

    with new_session() as s:
        s.add(ControlRequest(ts=NOW, kind="kill", source=source, reason="test"))
        s.commit()


@needs_db
async def test_self_test_kill_spares_the_paper_tracks_an_admin_kill_does_not(db: None) -> None:
    seed_decision()
    ex = make(ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)]), quote("99.9", "100"))
    await ex.tick()
    assert len(positions()) == 1 and len(positions("max")) == 1
    _kill("live_selftest")
    await ex.tick()
    assert ex.frozen
    assert {p.status for p in positions() + positions("max")} == {"open"}
    _kill("admin")
    await ex.tick()
    assert {p.close_reason for p in positions() + positions("max")} == {"kill"}


def test_timeline_helper() -> None:
    assert NOW + timedelta(hours=1) > NOW


@needs_db
async def test_entry_is_sized_down_to_free_cash_instead_of_skipped(db: None) -> None:
    """Five 20% shares plus fees do not fit five times: the last entry takes what is free."""
    from app.db.session import new_session

    seed_decision()  # plan: notional 2000 at 2x → margin 1000
    ex = make(ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)]), quote("99.9", "100"))
    with new_session() as s:
        ex.ledger(s, NOW)  # create the primary ledger, then leave it short of cash
        acct = s.get(PaperAccount, 1)
        assert acct is not None
        acct.cash = D("600")
        s.commit()
    await ex.tick()
    (pos,) = positions()
    assert pos.notional <= D("600") * 2 and pos.notional >= D("1000")  # fitted, not skipped
    assert pos.margin <= D("600")
