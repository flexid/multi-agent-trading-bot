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
    hard_stop: str | None = None,
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
                    **({"hard_stop": hard_stop} if hard_stop else {}),
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
    ex.mode = "paper"  # the config may have the pilot on; these tests script the paper book
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
    # three paper tracks open (primary, max, inverse); the inverse short does not stop out
    # at 97, so the closes are: primary (unreachable), max, primary again
    assert [c[0] for c in gw.calls] == ["open", "open", "open", "close", "close", "close"]


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


@needs_db
async def test_inverse_track_takes_the_other_side_with_mirrored_levels(db: None) -> None:
    seed_decision(direction="long", entry="100", stop="98", target="106")
    ex = make(ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)]), quote("99.9", "100"))
    await ex.tick()
    (primary,), (inverse,) = positions("primary"), positions("inverse")
    assert primary.direction == "long" and inverse.direction == "short"
    assert inverse.mode == "paper"
    # the same distances mirrored around the plan's entry (100): stop 102, target 94
    assert (primary.stop, primary.target) == (D(98), D(106))
    assert (inverse.stop, inverse.target) == (D(102), D(94))


@needs_db
async def test_soft_stop_waits_for_the_fifteen_minute_close(db: None) -> None:
    """A wick through the soft stop does not close; a 15-minute close beyond it does; the
    hard stop closes on touch."""
    from datetime import UTC, datetime

    seed_decision(stop="98", atr="1", hard_stop="97")  # hard stop one ATR further
    ex = make(ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)]), quote("99.9", "100"))
    await ex.tick()
    (pos,) = positions()
    assert pos.hard_stop == D(97)
    symbol = get_config().symbol(ASSET)
    # wick to 97.5 inside the same 15-minute bucket: open stays open
    ex.quotes[symbol] = quote("97.5", "97.6")
    await ex.tick()
    assert positions()[0].status == "open"
    # the bucket ends with the mid at 97.55: on the next pass the close is beyond 98
    bucket = int(datetime.now(UTC).timestamp() // 900)
    ex._candle_ref[symbol] = (bucket - 1, D("97.55"))
    await ex.tick()
    assert positions()[0].status == "closed" and positions()[0].close_reason == "stop"


@needs_db
async def test_aggressive_mode_banks_a_trade_at_its_share_of_the_goal(db: None) -> None:
    """Aggressive mode (owner 2026-10-11): a position is closed once its net profit reaches
    1% of the ledger's equity, long before its target; with the mode off it stays open."""
    from app.db.models import PaperAccount, RiskState
    from app.db.session import new_session

    seed_decision(stop="98", target="130")  # a far target: only the profit rule can close it
    ex = make(ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)]), quote("99.9", "100"))
    await ex.tick()
    (pos,) = positions()
    with new_session() as s:
        equity = s.get(PaperAccount, 1).equity  # type: ignore[union-attr]
    # the price move that makes the trade worth 1.2% of the ledger's equity, after its fees
    move = (equity * D("0.012") + pos.fees * 2) / pos.qty  # both fees and a margin
    symbol = get_config().symbol(ASSET)
    assert pos.entry_price is not None
    up = pos.entry_price + move
    ex.quotes[symbol] = quote(str(up), str(up + D("0.1")))
    await ex.tick()
    assert positions()[0].status == "open"  # mode off: nothing happens
    with new_session() as s:
        state = s.get(RiskState, 1) or RiskState(id=1, leverage_ceiling=D(2))
        state.aggressive = True
        s.merge(state)
        s.commit()
    try:
        await ex.tick()
        closed = positions()[0]
        assert closed.status == "closed" and closed.close_reason == "take_profit"
        # net of every cost, the exit fee included: at least the goal is banked
        assert closed.pnl is not None and closed.pnl >= equity * D("0.01")
    finally:
        with new_session() as s:
            after = s.get(RiskState, 1)
            if after is not None:
                after.aggressive = False
                s.commit()


@needs_db
async def test_hold_command_lengthens_open_trades_and_never_shortens(db: None) -> None:
    """The owner's "hold" command (2026-10-12): the executor itself raises the holding limit
    of the open trades; a smaller number changes nothing and nonsense is rejected."""
    from app.db.session import new_session

    seed_decision()
    ex = make(ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)]), quote("99.9", "100"))
    await ex.tick()
    before = positions()[0].max_hold_hours

    async def hold(hours: str) -> str | None:
        with new_session() as s:
            s.add(ControlRequest(ts=NOW, kind="hold", source="cli", reason=hours))
            s.commit()
        await ex.tick()
        with new_session() as s:
            last = s.scalars(select(ControlRequest).order_by(ControlRequest.id.desc())).first()
            return last.result if last else None

    result = await hold(str(before + 24))
    assert result is not None and result.endswith(f"held {before + 24} hours")
    assert {p.max_hold_hours for p in positions() + positions("max")} == {before + 24}
    await hold("1")  # shorter: nothing changes
    assert positions()[0].max_hold_hours == before + 24
    assert (await hold("999") or "").startswith("rejected")
    assert (await hold("soon") or "").startswith("rejected")
    assert positions()[0].status == "open" and not ex.frozen


@needs_db
async def test_aggressive_mode_banks_a_single_trade_at_its_margin_return(db: None) -> None:
    """Owner 2026-10-12: a trade is closed on its own once it is 2% up on its margin, net
    of the closing fee; at 1.5% it stays open. The book as a whole is nowhere near its goal."""
    from app.config import get_config as cfg_
    from app.db.models import RiskState
    from app.db.session import new_session

    seed_decision(stop="98", target="130")
    ex = make(ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)]), quote("99.9", "100"))
    await ex.tick()
    (pos,) = [p for p in positions() if p.track == "primary"]
    assert pos.entry_price is not None and pos.leverage >= 1
    level = cfg_().aggressive.take_profit_trade_pct
    assert level == D("0.02")
    with new_session() as s:
        state = s.get(RiskState, 1) or RiskState(id=1, leverage_ceiling=D(2))
        state.aggressive = True
        s.merge(state)
        s.commit()
    try:
        symbol = get_config().symbol(ASSET)
        fees_pct = pos.fees * 2 / pos.notional  # entry fee paid, exit fee to come
        # the return on margin is the price move times the leverage, after both fees
        # armed at 2%, the take trails: a run to 4% stays open, a drop to 3% (a quarter
        # given back) closes it; a trade that crosses 2% and turns is banked at the level
        moves = (
            (D("0.015"), "open"),
            (D("0.021"), "open"),
            (D("0.040"), "open"),
            (D("0.032"), "open"),
            (D("0.029"), "closed"),
        )
        for ret, expect in moves:
            up = pos.entry_price * (1 + ret / pos.leverage + fees_pct)
            ex.quotes[symbol] = quote(str(up), str(up + D("0.01")))
            await ex.tick()
            (cur,) = [p for p in positions() if p.track == "primary"]
            assert cur.status == expect, ret
        assert cur.close_reason == "take_profit"
        assert cur.pnl is not None and cur.pnl / pos.margin >= D("0.028") >= level
        assert cur.profit_peak_pct is not None and cur.profit_peak_pct >= D("0.039")
    finally:
        with new_session() as s:
            cur_state = s.get(RiskState, 1)
            if cur_state is not None:
                cur_state.aggressive = False
                s.commit()


@needs_db
async def test_aggressive_mode_banks_the_whole_ledger_at_the_day_goal(db: None) -> None:
    """The day goal (owner 2026-10-12): the 1% is measured on the ledger for the day, not on
    one trade. A ledger already up most of the way closes its open trade as soon as the
    total reaches the goal, and the next goal counts from the banked equity."""
    from app.db.models import PaperAccount, RiskState
    from app.db.session import new_session

    seed_decision(stop="98", target="130")
    ex = make(ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)]), quote("99.9", "100"))
    await ex.tick()
    mine = [p for p in positions() if p.track == "primary"]
    assert len(mine) == 1 and mine[0].status == "open"
    with new_session() as s:
        acct = s.get(PaperAccount, 1)
        assert acct is not None
        # earlier trades made most of the day's goal: this one is a few fees short of it
        acct.goal_base = (acct.equity + mine[0].fees * 3) / D("1.01")
        base = acct.goal_base
        state = s.get(RiskState, 1) or RiskState(id=1, leverage_ceiling=D(2))
        state.aggressive = True
        s.merge(state)
        s.commit()
    try:
        await ex.tick()
        assert [p.status for p in positions() if p.track == "primary"] == ["open"]
        # a small move in favour covers the gap and the closing fee
        symbol = get_config().symbol(ASSET)
        assert mine[0].entry_price is not None
        up = mine[0].entry_price + mine[0].fees * 6 / mine[0].qty
        ex.quotes[symbol] = quote(str(up), str(up + D("0.01")))
        await ex.tick()
        (closed,) = [p for p in positions() if p.track == "primary"]
        assert closed.status == "closed" and closed.close_reason == "take_profit"
        with new_session() as s:
            after = s.get(PaperAccount, 1)
            assert after is not None and after.goal_base is not None
            assert after.goal_base >= base * D("1.01")  # the next step counts from here
            assert after.cash >= base * D("1.01") - D("0.01")  # and that much is banked
        # the scheduler is asked for a new decision cycle; the executor leaves that request
        # alone on its next passes
        from app.db.models import ControlRequest
        from app.decision import goal_cycle

        await ex.tick()
        with new_session() as s:
            (req,) = s.scalars(select(ControlRequest)).all()
            assert req.kind == "cycle" and req.applied_at is None
            cfg = get_config()
            # two minutes before the regular cycle: not worth an extra one
            near = goal_cycle.next_scheduled(NOW, cfg.trading.cycle_hours) - timedelta(minutes=2)
            assert goal_cycle.claim(s, cfg, near) is None
            assert req.result is not None and req.result.startswith("skipped: the regular")
            # a new request two hours before it: the cycle starts
            goal_cycle.request(s, NOW, "primary")
            s.commit()
            far = goal_cycle.next_scheduled(NOW, cfg.trading.cycle_hours) - timedelta(hours=2)
            assert goal_cycle.claim(s, cfg, far) == "day goal banked on primary"
            assert goal_cycle.claim(s, cfg, far) is None  # settled: it does not start twice
    finally:
        with new_session() as s:
            cur = s.get(RiskState, 1)
            if cur is not None:
                cur.aggressive = False
                s.commit()


@needs_db
async def test_a_backup_stop_without_a_position_is_cancelled(db: None) -> None:
    """A stop order left on the exchange with no open position behind it (2026-10-11: a buy
    stop for a DOGE short closed hours before) is cancelled, on the second round it is seen
    unowned; a stop an open position owns is left alone."""
    from datetime import UTC, datetime, timedelta

    from app.db.session import new_session

    ex = make(ScriptedGateway(opens=[]), quote("99.9", "100"))
    cancelled: list[tuple[str, str]] = []

    async def resting() -> list[tuple[str, str]]:
        return [("DOGEUSDT", "bk-orphan"), ("BTCUSDT", "bk-owned")]

    async def cancel(symbol: str, link: str) -> bool:
        cancelled.append((symbol, link))
        return True

    ex.gateway.supports_backup_stop = True
    ex.gateway.resting_backup_stops = resting  # type: ignore
    ex.gateway.cancel_backup_stop = cancel  # type: ignore
    ex._alert = lambda *a, **k: None  # type: ignore
    now = datetime.now(UTC)
    with new_session() as s:
        s.add(
            Position(
                mode="pilot",
                track="pilot",
                asset="BTC",
                symbol="BTCUSDT",
                direction="short",
                status="open",
                qty=D(1),
                entry_price=D(100),
                leverage=D(1),
                margin=D(100),
                notional=D(100),
                stop=D(110),
                target=D(90),
                max_hold_hours=24,
                opened_at=now,
                backup_stop_link="bk-owned",
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()
        assert await ex.cancel_orphan_backups(s, now) == 0  # first sighting: wait a round
        assert await ex.cancel_orphan_backups(s, now + timedelta(seconds=5)) == 0  # throttled
        assert await ex.cancel_orphan_backups(s, now + timedelta(seconds=61)) == 1
    assert cancelled == [("DOGEUSDT", "bk-orphan")]


def test_the_pilot_has_a_slot_for_every_asset() -> None:
    """Ten positions sized per sleeve fit the pilot's capital together (owner 2026-10-11)."""
    cfg = get_config()
    ex = Executor(cfg, get_secrets(), ScriptedGateway(opens=[]), feed=False)
    slots = {a: ex.pilot_slot(a) for a in cfg.trading.assets}
    cap = cfg.pilot.capital_usdt
    assert slots["BTC"] == cap * D("0.70") * D("0.20") and slots["PEPE"] == cap * D("0.30") * D(
        "0.20"
    )
    assert sum(slots.values()) <= cap
    assert ex.pilot_slot("BNB") == cap * D("0.20")  # outside the sleeves: the old share
