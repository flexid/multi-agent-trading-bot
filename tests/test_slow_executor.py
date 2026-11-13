"""The executor's slow path (v16, docs/RETHINK_2026-11.md): a slow book opens on its daily
rule plan, holds through everything the fast book would have sold into, and closes when
the plan no longer wants the side. Scripted gateway, injected quotes, the test Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.config import Config, StrategyConfig, get_config, get_secrets
from app.db.models import (
    Candle,
    ControlRequest,
    Cycle,
    DecisionRecord,
    EquitySnapshot,
    PaperAccount,
    ParamChange,
    Position,
    RulePlan,
    XPostOut,
    XPostRecord,
)
from app.db.session import new_session
from app.execution.executor import Executor
from app.execution.gateway import OrderOutcome, Outcome, ScriptedGateway
from app.execution.simulator import Quote
from tests.test_data_layer import needs_db
from tests.test_executor_failures import NOW, positions, seed_decision

BTC = get_config().symbol("BTC")


def cfg_with(**strategy: object) -> Config:
    return get_config().model_copy(update={"strategy": StrategyConfig(enabled=True, **strategy)})


CFG = cfg_with()


@pytest.fixture
def db() -> Iterator[None]:
    def clean() -> None:
        with new_session() as s:
            for model in (
                XPostOut,
                ControlRequest,
                RulePlan,
                Position,
                DecisionRecord,
                Cycle,
                PaperAccount,
            ):
                s.execute(delete(model))
            s.commit()

    clean()
    yield
    clean()


def quote(bid: str, ask: str) -> Quote:
    return Quote(D(bid), D(ask), datetime.now(UTC))


def executor(cfg: Config = CFG, bid: str = "99.95", ask: str = "100") -> Executor:
    ex = Executor(
        cfg, get_secrets(), ScriptedGateway(opens=[OrderOutcome(Outcome.FILLED)]), feed=False
    )
    ex.mode = "paper"
    ex.posting_live = lambda: False  # type: ignore[method-assign]
    ex.quotes[BTC] = quote(bid, ask)
    ex.qty_steps[BTC] = D("0.001")
    return ex


_day = [0]


def rule_plan(
    book: str,
    track: str,
    side: str,
    *,
    action: str = "open",
    ts: datetime | None = None,
    entry: str = "100",
    notional: str = "1000",
    atr: str | None = None,
) -> int:
    """One daily row for BTC. Every call gets its own day, as the real cycle writes one a day."""
    _day[0] += 1
    plan = None
    if action == "open":
        long = side == "long"
        e = D(entry)
        stop = e * (D("0.85") if long else D("1.15"))
        plan = {
            "side": side,
            "entry": entry,
            "stop": str(stop),
            "hard_stop": str(stop),
            "target": str(e * 1000 if long else e / 1000),
            "max_hold_hours": 43800,
            "leverage": "1",
            "borrow": not long,
            "notional": notional,
            "margin": notional,
            **({"atr": atr} if atr else {}),
        }
    with new_session() as s:
        row = RulePlan(
            ts=ts or NOW,
            day=(NOW - timedelta(days=400 - _day[0])).date(),
            book=book,
            track=track,
            asset="BTC",
            side=side,
            action=action,
            plan=plan,
        )
        s.add(row)
        s.commit()
        return row.id


def ledger(track: str) -> PaperAccount:
    with new_session() as s:
        acct = s.scalars(select(PaperAccount).where(PaperAccount.track == track)).one()
        s.expunge(acct)
        return acct


@needs_db
async def test_a_rule_plan_opens_one_whole_position_on_its_books_ledger(db: None) -> None:
    plan_id = rule_plan("trend", "trend", "long")
    ex = executor()
    await ex.tick()
    (pos,) = positions("trend")
    assert pos.book == "trend" and pos.plan_id == plan_id
    assert pos.decision_id is None and pos.cycle_id is None  # no managers' decision behind it
    assert pos.direction == "long" and pos.leverage == 1 and pos.status == "open"
    assert pos.entry_price == D(100) and pos.margin == pos.notional == D(1000)
    assert pos.stop == pos.hard_stop == D(85) and pos.target == D(100000)  # one disaster stop
    assert pos.max_hold_hours == 43800
    assert positions("primary") == [] and positions("short") == [] and positions("seats") == []
    acct = ledger("trend")
    assert acct.id == 7 and acct.starting_capital == CFG.trading.capital_max_usdt
    assert (
        acct.cash == acct.starting_capital - pos.margin
    )  # the margin left; no leverage, no borrow
    await ex.tick()  # the same plan is never opened twice
    assert len(positions("trend")) == 1
    # a plan older than one cycle is stale: the next day's cycle decides again
    with new_session() as s:
        s.execute(delete(Position))
        s.commit()
    rule_plan("short", "short", "short", ts=NOW - timedelta(hours=5))
    await ex.tick()
    assert positions("short") == []


@needs_db
async def test_a_slow_position_is_exempt_from_the_take_the_day_goal_and_the_trail(db: None) -> None:
    """The fast book beside it, on the same price path, is banked at its 2% take; the slow
    position is still there (the take turned +68% a year into -83% on this signal)."""
    seed_decision(stop="90", target="140")  # the managers' fast book: long BTC on primary
    rule_plan("trend", "trend", "long")
    ex = executor()
    await ex.tick()
    (fast,) = positions("primary")
    (slow,) = positions("trend")
    assert fast.book is None and slow.book == "trend"
    ex.quotes[BTC] = quote("104.9", "105")  # +5%: the fast book's 2% take is armed and trails
    await ex.tick()
    assert positions("primary")[0].profit_peak_pct is not None
    ex.quotes[BTC] = quote("102.4", "102.5")  # it gives half back: the fast trade is banked
    await ex.tick()
    (fast,) = positions("primary")
    (slow,) = positions("trend")
    assert fast.status == "closed" and fast.close_reason == "take_profit"
    assert slow.status == "open" and slow.trail_stop is None and slow.profit_peak_pct is None
    ex.quotes[BTC] = quote(
        "129.9", "130"
    )  # +30%: a fast trade would trail here; the slow one holds
    await ex.tick()
    ex.quotes[BTC] = quote("109.9", "110")  # and it gives a lot back without being closed
    await ex.tick()
    (slow,) = positions("trend")
    assert slow.status == "open" and slow.trail_stop is None
    assert slow.mfe_pct is not None and slow.mfe_pct > D("0.29")  # the excursion is still recorded
    # the trend ledger stands far above any day goal and may still open: no gate for a slow book
    assert ledger("trend").equity > CFG.trading.capital_max_usdt
    assert ledger("trend").goal_base is None  # and the day goal never took it for banked


@needs_db
async def test_the_signal_closes_a_slow_position_and_the_ledger_books_it(db: None) -> None:
    rule_plan("trend", "trend", "long")
    ex = executor()
    await ex.tick()
    (pos,) = positions("trend")
    start = ledger("trend").starting_capital
    ex.quotes[BTC] = quote("107.9", "108")
    rule_plan("trend", "trend", "long", action="hold", ts=datetime.now(UTC))  # the next day: hold
    await ex.tick()
    assert positions("trend")[0].status == "open"
    rule_plan("trend", "trend", "flat", action="close", ts=datetime.now(UTC))  # the trend turned
    await ex.tick()
    (closed,) = positions("trend")
    assert closed.status == "closed" and closed.close_reason == "signal"
    assert closed.exit_price == D("107.9") and closed.pnl is not None and closed.pnl > 0
    acct = ledger("trend")
    assert abs(acct.cash - (start + closed.pnl)) < D("0.000001")  # one round trip, booked once
    await ex.tick()  # a close row opens nothing
    assert len(positions("trend")) == 1


@needs_db
async def test_a_flip_closes_the_old_side_and_opens_the_new_one(db: None) -> None:
    rule_plan("challenger", "seats", "long")  # the managers said long
    ex = executor()
    await ex.tick()
    (long_,) = positions("seats")
    assert long_.book == "challenger" and long_.direction == "long"
    rule_plan("challenger", "seats", "short", ts=datetime.now(UTC))  # today they say short
    await ex.tick()
    old, new = positions("seats")
    assert old.status == "closed" and old.close_reason == "signal"
    assert new.status == "open" and new.direction == "short" and new.book == "challenger"
    assert new.entry_price is not None
    assert new.stop == new.hard_stop and new.stop > new.entry_price  # the disaster stop, above
    assert new.borrowed == new.qty  # a short borrows the coin, at 1x


@needs_db
async def test_the_disaster_stop_fires_on_touch(db: None) -> None:
    rule_plan("trend", "trend", "long")
    ex = executor()
    await ex.tick()
    ex.quotes[BTC] = quote("86", "86.1")
    await ex.tick()
    assert positions("trend")[0].status == "open"
    ex.quotes[BTC] = quote("84.9", "85")
    await ex.tick()
    (pos,) = positions("trend")
    assert pos.status == "closed" and pos.close_reason == "stop_hard"
    assert pos.pnl is not None and pos.pnl < D(-150)  # about 15% of the 1,000 put in


@needs_db
async def test_the_takeover_switch_hosts_the_trend_book_on_primary_and_closes_the_fast_book(
    db: None,
) -> None:
    switched = cfg_with(main_book="trend")
    seed_decision(stop="90", target="140")  # a managers' plan: nobody opens it any more
    rule_plan("trend", "primary", "long")
    ex = executor(switched)
    assert ex.rule_tracks == {"primary": "trend", "short": "short", "seats": "challenger"}
    await ex.tick()
    (pos,) = positions("primary")
    assert pos.book == "trend" and pos.decision_id is None
    assert positions("control") == [] and positions("trend") == []
    ex.quotes[BTC] = quote("104.9", "105")  # the public record's position holds through +5%
    await ex.tick()
    assert positions("primary")[0].status == "open"
    # before the switch the same two instructions land on their own ledgers
    with new_session() as s:
        s.execute(delete(Position))
        s.execute(delete(PaperAccount))
        s.commit()
    rule_plan("trend", "trend", "long")
    before = executor()
    await before.tick()
    assert [p.book for p in positions("primary")] == [None]
    assert [p.book for p in positions("trend")] == ["trend"]


def test_a_rule_track_without_a_ledger_is_refused_at_start() -> None:
    with pytest.raises(ValueError, match="no ledger"):
        Executor(
            cfg_with(long_track="nowhere"), get_secrets(), ScriptedGateway(opens=[]), feed=False
        )


def test_without_the_strategy_the_executor_runs_the_fast_book_alone() -> None:
    off = get_config().model_copy(update={"strategy": StrategyConfig()})
    ex = Executor(off, get_secrets(), ScriptedGateway(opens=[]), feed=False)
    ex.mode = "paper"
    assert ex.rule_tracks == {} and set(ex.tracks()) == {"primary", "control"}


@needs_db
async def test_a_book_that_moves_ledgers_or_is_switched_off_closes_its_old_positions(
    db: None,
) -> None:
    """The takeover moves the trend book from its paper ledger to primary: the position on
    the old ledger would never see a daily row again, so the executor closes it; the book
    opens again on its new ledger from the next plan. The same when a book is switched off."""
    rule_plan("trend", "trend", "long")
    rule_plan("short", "short", "short", entry="100")
    before = executor()
    await before.tick()
    assert [p.status for p in positions("trend")] == ["open"]
    assert [p.status for p in positions("short")] == ["open"]
    after = executor(cfg_with(main_book="trend", short_track=""))  # the switch; no short book
    await after.tick()
    (old,) = positions("trend")
    assert old.status == "closed" and old.close_reason == "signal"
    (short,) = positions("short")
    assert short.status == "closed" and short.close_reason == "signal"
    assert positions("primary") == []  # yesterday's plan was for the old ledger
    # nobody runs these two ledgers any more: their record ends on what they banked, not
    # on the last mark they had while the positions were open
    for track, closed in (("trend", old), ("short", short)):
        acct = ledger(track)
        assert closed.pnl is not None
        assert acct.equity == acct.cash == acct.starting_capital + closed.pnl
        with new_session() as s:
            last = s.scalars(
                select(EquitySnapshot)
                .where(EquitySnapshot.mode == f"paper:{track}")
                .order_by(EquitySnapshot.ts.desc())
                .limit(1)
            ).one()
        assert last.equity == acct.cash and last.open_positions == 0
    rule_plan("trend", "primary", "long", ts=datetime.now(UTC))
    await after.tick()
    (new,) = positions("primary")
    assert new.book == "trend" and new.status == "open"


@needs_db
async def test_a_short_pays_borrow_interest_for_every_whole_hour_it_is_open(db: None) -> None:
    """Found in the review of v16: the interest clock was the row's ``updated_at``, which
    every pass moves, so no hour ever passed and a paper short never paid for its borrow.
    A slow short is held for weeks; the clock is the position's own now."""
    rule_plan("short", "short", "short")
    rule_plan("trend", "trend", "long")
    ex = executor()
    ex.borrow_rates[BTC] = D("0.0001")  # a hundredth of a percent an hour
    await ex.tick()
    (short,) = positions("short")
    (long_,) = positions("trend")
    assert short.borrowed == short.qty and long_.borrowed == 0
    await ex.tick()  # seconds later: nothing to book yet
    assert positions("short")[0].interest == 0
    opened = datetime.now(UTC) - timedelta(hours=3, minutes=10)
    with new_session() as s:
        for row in s.scalars(select(Position)).all():
            row.opened_at, row.interest_at = opened, None  # as if opened three hours ago
        s.commit()
    await ex.tick()
    (short,) = positions("short")
    owed = short.qty * ex.quotes[BTC].mid * D("0.0001") * 3  # the coin borrowed, at today's price
    assert owed > 0 and abs(short.interest - owed) < D("0.000001")
    assert short.interest_at == opened + timedelta(hours=3)  # booked up to the last whole hour
    assert positions("trend")[0].interest == 0  # a long at 1x borrows nothing
    await ex.tick()  # the same hour is never charged twice
    assert positions("short")[0].interest == short.interest
    # the interest comes out of the result and is on the ledger when the position closes
    rule_plan("short", "short", "flat", action="close", ts=datetime.now(UTC))
    await ex.tick()
    (closed,) = positions("short")
    assert closed.status == "closed" and closed.close_reason == "signal"
    assert abs(ledger("short").interest_paid - owed) < D("0.000001")
    assert closed.entry_price is not None and closed.exit_price is not None
    price_only = (closed.entry_price - closed.exit_price) * closed.qty
    assert closed.pnl is not None and closed.pnl < price_only - owed  # fees come off as well


def closed_position(book: str | None, track: str, pnl: str, when: datetime) -> None:
    with new_session() as s:
        s.add(
            Position(
                book=book,
                mode="paper",
                track=track,
                asset="BTC",
                symbol=BTC,
                direction="long",
                status="closed",
                qty=D(10),
                entry_price=D(100),
                exit_price=D(90),
                leverage=D(1),
                margin=D(1000),
                notional=D(1000),
                stop=D(85),
                target=D(100000),
                max_hold_hours=43800,
                opened_at=when - timedelta(days=20),
                closed_at=when,
                close_reason="signal",
                pnl=D(pnl),
                created_at=when,
                updated_at=when,
            )
        )
        s.commit()


@needs_db
def test_a_slow_books_losing_close_does_not_cap_the_fast_books_leverage(db: None) -> None:
    """The risk engine halves the fast book's leverage after two losing days. A trend
    position that ends on its signal is not one of its days."""
    from app.risk.apply import losing_days_in_row

    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for days in (1, 2):
        closed_position("trend", "trend", "-100", midnight - timedelta(days=days, hours=-12))
    with new_session() as s:
        assert losing_days_in_row(s, now) == 0
    for days in (1, 2):  # the same two days lost on the managers' own book do count
        closed_position(None, "primary", "-10", midnight - timedelta(days=days, hours=-12))
    with new_session() as s:
        assert losing_days_in_row(s, now) == 2


def test_a_slow_book_only_ever_gets_a_paper_ledger_of_its_own() -> None:
    from pydantic import ValidationError

    for bad in (
        {"short_track": "pilot"},  # real money
        {"challenger_track": "live"},
        {"long_track": "control"},  # the fast book's control ledger
        {"long_track": "primary"},  # only the takeover switch puts a book there
        {"long_track": "trend", "short_track": "trend"},  # two books, one ledger
        {"long_track": ""},  # the champion always has one
    ):
        with pytest.raises(ValidationError):
            StrategyConfig(enabled=True, **bad)
    # and whatever the process trades for real, a slow book's ledger is stored as paper
    ex = Executor(CFG, get_secrets(), ScriptedGateway(opens=[]), feed=False)
    ex.mode = "pilot"
    assert ex.tracks() == {
        "primary": "paper",
        "pilot": "pilot",
        "control": "paper",
        "trend": "paper",
        "short": "paper",
        "seats": "paper",
    }
    assert ex.gateway_for(ex.tracks()["short"]) is ex.paper_gateway


@needs_db
async def test_a_plan_far_from_the_market_keeps_its_levels_as_shares_of_the_entry(
    db: None,
) -> None:
    """The fast book shifts a plan's stop and target by the distance to spot. A slow
    short's target sits a thousandth of the entry away from zero: shifted by three
    dollars it would be negative, and its disaster stop would no longer be 15% away."""
    rule_plan("short", "short", "short", entry="100")  # yesterday's close; the market is at 97
    ex = executor(bid="96.95", ask="97.05")
    await ex.tick()
    (pos,) = positions("short")
    assert pos.entry_price == D("96.95")  # a short sells at the bid
    assert pos.stop == pos.hard_stop == D("111.55")  # 115 x 0.97: still 15% above the mid
    assert pos.target == D("0.097") and pos.target > 0
    assert pos.status == "open"


@needs_db
async def test_a_news_shock_is_measured_against_todays_range_on_a_slow_position(
    db: None,
) -> None:
    """A slow position is weeks old; the ATR on its row is the one of its entry day. The
    shock exit asks whether the price moved one ATR of the market as it is now."""
    posted = datetime.now(UTC) - timedelta(minutes=5)

    def clean() -> None:
        with new_session() as s:
            s.execute(delete(XPostRecord).where(XPostRecord.id == "shock-slow-1"))
            s.execute(
                delete(Candle).where(Candle.symbol == BTC, Candle.interval.in_(["15", "240"]))
            )
            s.commit()

    clean()
    rule_plan("trend", "trend", "long", atr="0.1")  # a very quiet entry day
    ex = executor()
    await ex.tick()
    (pos,) = positions("trend")
    assert pos.atr == D("0.1")
    try:
        with new_session() as s:
            for i in range(15):  # today's four-hour candles range two dollars each
                s.add(
                    Candle(
                        symbol=BTC,
                        interval="240",
                        open_time=posted - timedelta(hours=4 * (i + 1)),
                        open=D(100),
                        high=D(101),
                        low=D(99),
                        close=D(100),
                        volume=D(1),
                        turnover=D(1),
                        fetched_at=posted,
                    )
                )
            s.add(
                Candle(
                    symbol=BTC,
                    interval="15",
                    open_time=posted - timedelta(minutes=10),
                    open=D(100),
                    high=D(100),
                    low=D(100),
                    close=D(100),
                    volume=D(1),
                    turnover=D(1),
                    fetched_at=posted,
                )
            )
            s.add(
                XPostRecord(
                    id="shock-slow-1",
                    author="test",
                    created_at=posted,
                    fetched_at=posted,
                    text="[test]",
                    asset="BTC",
                    stance="bearish",
                    kind="news",
                    credibility=D("0.9"),
                    shock=True,
                    fresh=True,
                    labeled_at=posted,
                )
            )
            s.commit()
        ex.quotes[BTC] = quote("98.95", "99.05")  # down one: ten entry-day ATRs, half of today's
        await ex.tick()
        assert positions("trend")[0].status == "open"
        ex.quotes[BTC] = quote("97.85", "97.95")  # down more than today's two-dollar range
        await ex.tick()
        (closed,) = positions("trend")
        assert closed.status == "closed" and closed.close_reason == "shock"
    finally:
        clean()


@needs_db
async def test_a_config_reload_never_leaves_the_books_half_switched(
    db: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An admin parameter change makes the executor read config.toml again. If the owner
    had already set the takeover switch there, the fast book would stop opening while the
    trend book still traded on its old ledger. The layout is the one of the start; the
    switch waits for the restart, and the log says so."""
    import logging

    import app.config as config_module

    # alembic's logging setup (the test database's migration) switches existing loggers off
    monkeypatch.setattr(logging.getLogger("executor"), "disabled", False)
    path = tmp_path / "config.toml"
    path.write_text(config_module.CONFIG_PATH.read_text())
    switched = cfg_with(main_book="trend")

    class Reloaded:
        def __call__(self) -> Config:
            return switched

        def cache_clear(self) -> None:
            pass

    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    monkeypatch.setattr(config_module, "get_config", Reloaded())
    seed_decision(stop="90", target="140")  # the managers' plan: still theirs to open
    rule_plan("trend", "trend", "long")
    with new_session() as s:
        s.execute(delete(ParamChange))
        s.add(
            ParamChange(
                ts=datetime.now(UTC),
                key="risk.chase_max_atr",
                old_value="1",
                new_value="1",
                requested_by="test",
            )
        )
        s.commit()
    ex = executor()
    try:
        with caplog.at_level("WARNING", logger="executor"):
            await ex.tick()
        with new_session() as s:
            assert s.scalars(select(ParamChange)).one().result == "applied"
        assert "until the executor and the scheduler are restarted" in caplog.text
        assert ex.slow is False and ex.cfg.strategy.main_book == "managers"
        assert ex.rule_tracks == {"trend": "trend", "short": "short", "seats": "challenger"}
        assert [p.book for p in positions("primary")] == [None]  # the fast book still opens
        assert [p.book for p in positions("trend")] == ["trend"]  # and the trend book stays put
    finally:
        with new_session() as s:
            s.execute(delete(ParamChange))
            s.commit()
