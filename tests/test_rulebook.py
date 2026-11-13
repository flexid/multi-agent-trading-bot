"""The slow books' daily rule cycle (app/risk/rulebook.py): what each book wants, the
executor's instruction, and the rules around it (idempotent, fail closed, sit out)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import delete, select

from app.config import Config, StrategyConfig, get_config
from app.db.models import Candle, Cycle, DecisionRecord, PaperAccount, Position, RulePlan
from app.db.session import new_session
from app.risk import rulebook
from tests.test_data_layer import needs_db

NOW = datetime(2026, 11, 12, 0, 16, tzinfo=UTC)  # the rule cycle's minute
TODAY = datetime(2026, 11, 12, tzinfo=UTC)
YESTERDAY = (TODAY - timedelta(days=1)).date()


def cfg_with(**strategy: object) -> Config:
    base = get_config()
    return base.model_copy(update={"strategy": StrategyConfig(enabled=True, **strategy)})


CFG = cfg_with()
ASSETS = CFG.trading.assets
SYMBOLS = [CFG.symbol(a) for a in ASSETS]


@pytest.fixture
def db() -> Iterator[None]:
    def clean() -> None:
        with new_session() as s:
            s.execute(delete(RulePlan))
            s.execute(delete(Position).where(Position.book.is_not(None)))
            s.execute(delete(Candle).where(Candle.interval == "D", Candle.symbol.in_(SYMBOLS)))
            s.execute(delete(Cycle).where(Cycle.trigger == "rulebook-test"))
            s.execute(
                delete(PaperAccount).where(PaperAccount.track.in_(("trend", "short", "seats")))
            )
            s.commit()

    clean()
    yield
    clean()


def seed(
    asset: str, daily: float, *, days: int = 80, last: datetime = TODAY - timedelta(days=1)
) -> None:
    """``days`` closed daily candles ending with the one that opened at ``last``, each
    close ``daily`` above the one before."""
    with new_session() as s:
        price = 100.0
        for i in range(days):
            price *= 1 + daily
            s.add(
                Candle(
                    symbol=CFG.symbol(asset),
                    interval="D",
                    open_time=last - timedelta(days=days - 1 - i),
                    open=D(str(price)),
                    high=D(str(price * 1.01)),
                    low=D(str(price * 0.99)),
                    close=D(str(price)),
                    volume=D(1),
                    turnover=D(1),
                    fetched_at=NOW,
                )
            )
        s.commit()


def seed_all(up: tuple[str, ...] = ("BTC",), down: tuple[str, ...] = ("ETH",)) -> None:
    for a in ASSETS:
        seed(a, 0.01 if a in up else -0.01 if a in down else 0.0)


def position(book: str, track: str, asset: str, side: str, **kw: object) -> int:
    with new_session() as s:
        base: dict[str, object] = dict(
            mode="paper",
            track=track,
            book=book,
            asset=asset,
            symbol=CFG.symbol(asset),
            direction=side,
            status="open",
            qty=D(1),
            entry_price=D(100),
            leverage=D(1),
            margin=D(100),
            notional=D(100),
            stop=D(85),
            target=D(100000),
            max_hold_hours=43800,
            opened_at=NOW - timedelta(days=3),
            created_at=NOW - timedelta(days=3),
            updated_at=NOW - timedelta(days=3),
        )
        base.update(kw)
        p = Position(**base)
        s.add(p)
        s.commit()
        return p.id


def rows(now: datetime = NOW, cfg: Config = CFG) -> dict[tuple[str, str], RulePlan]:
    with new_session() as s:
        rulebook.decide(s, cfg, now)
    with new_session() as s:
        day = (rulebook.day_start(now) - timedelta(days=1)).date()
        return {
            (r.book, r.asset): r
            for r in s.scalars(select(RulePlan).where(RulePlan.day == day)).all()
        }


def cycle(started: datetime, calls: dict[str, str], status: str = "done") -> None:
    with new_session() as s:
        c = Cycle(
            started_at=started,
            kind="scheduled",
            trigger="rulebook-test",
            mode="shadow",
            status=status,
        )
        s.add(c)
        s.flush()
        for asset, direction in calls.items():
            s.add(
                DecisionRecord(
                    cycle_id=c.id,
                    asset=asset,
                    ts=started,
                    valid_agents=5,
                    agreement="agree" if direction != "flat" else "flat",
                    direction=direction,
                    reason="test",
                )
            )
        s.commit()


@needs_db
def test_each_book_gets_its_instruction_once_a_day(db: None) -> None:
    seed_all()
    out = rows()
    long_ = out[("trend", "BTC")]
    assert long_.side == "long" and long_.action == "open" and long_.day == YESTERDAY
    assert long_.plan is not None and long_.basis is not None
    assert long_.basis["votes"] == [1, 1, 1] and long_.basis["signal"] == "long"
    p = long_.plan
    assert p["side"] == "long" and p["leverage"] == "1" and p["borrow"] is False
    entry = D(p["entry"])
    assert D(p["stop"]) == D(p["hard_stop"]) == entry * D("0.85")  # the one disaster stop
    assert D(p["target"]) == entry * 1000 and p["max_hold_hours"] == 43800  # the signal is the exit
    # a steady 1% a day is calmer than the target: the whole slot, one sixth of the paper capital
    assert D(p["notional"]) == (CFG.trading.capital_max_usdt / len(ASSETS)).quantize(D("0.01"))
    assert D(p["margin"]) == D(p["notional"])
    assert out[("short", "BTC")].side == "flat" and out[("short", "BTC")].action == "none"
    short = out[("short", "ETH")]
    assert short.side == "short" and short.action == "open" and short.plan is not None
    assert short.plan["borrow"] is True and D(short.plan["stop"]) == D(short.plan["entry"]) * D(
        "1.15"
    )
    assert D(short.plan["target"]) == D(short.plan["entry"]) / 1000
    assert out[("trend", "ETH")].action == "none"
    flat = out[("trend", "SOL")]  # no move at all: every vote is zero, nobody wants it
    assert flat.side == "flat" and flat.action == "none" and out[("short", "SOL")].action == "none"
    assert not any(book == "challenger" for book, _ in out)  # no managers' cycle today
    # the same minute again, and a retry half an hour later: nothing new
    with new_session() as s:
        assert rulebook.decide(s, CFG, NOW) == []
        assert rulebook.decide(s, CFG, NOW + timedelta(minutes=30)) == []
        assert s.scalar(select(RulePlan.id).where(RulePlan.book == "challenger")) is None


@needs_db
def test_a_missing_daily_candle_writes_nothing_for_that_asset(db: None) -> None:
    for a in ASSETS:
        seed(a, 0.01, last=TODAY - timedelta(days=2 if a == "BTC" else 1))
    with new_session() as s:
        assert rulebook.missing_candles(s, CFG, NOW) == ["BTC"]
    out = rows()
    assert ("trend", "BTC") not in out and ("short", "BTC") not in out  # fail closed
    assert out[("trend", "ETH")].action == "open"
    seed("XRP", 0.01, days=30, last=TODAY - timedelta(days=200))  # stray old rows do not matter
    # the candle arrives: a later run of the same day writes the missing rows only
    with new_session() as s:
        s.add(
            Candle(
                symbol=CFG.symbol("BTC"),
                interval="D",
                open_time=TODAY - timedelta(days=1),
                open=D(300),
                high=D(303),
                low=D(297),
                close=D(300),
                volume=D(1),
                turnover=D(1),
                fetched_at=NOW,
            )
        )
        s.commit()
        new = rulebook.decide(s, CFG, NOW + timedelta(minutes=30))
        assert {(r.book, r.asset) for r in new} == {("trend", "BTC"), ("short", "BTC")}


@needs_db
def test_hold_close_and_the_size_follows_the_ledger_and_the_volatility(db: None) -> None:
    seed_all(up=("BTC", "XRP"), down=("ETH",))
    position("trend", "trend", "BTC", "long")  # already long: hold
    position("trend", "trend", "ETH", "long")  # the trend turned: close
    with new_session() as s:
        s.add(
            PaperAccount(
                id=7,
                track="trend",
                starting_capital=D(10000),
                cash=D(6000),
                equity=D(12000),
                day_start_equity=D(12000),
                day_date=TODAY,
                day_high_equity=D(12000),
                updated_at=NOW,
            )
        )
        s.commit()
    out = rows()
    assert out[("trend", "BTC")].action == "hold" and out[("trend", "BTC")].plan is None
    assert out[("trend", "ETH")].side == "flat" and out[("trend", "ETH")].action == "close"
    xrp = out[("trend", "XRP")]
    assert xrp.action == "open" and xrp.plan is not None
    assert D(xrp.plan["notional"]) == D(2000)  # the ledger's equity of 12,000 over six assets
    # a wilder asset gets a smaller share of its slot: the weight is vol_target / volatility
    plan = rulebook.plan_for(CFG, "long", 100.0, 0.4, D(12000))
    assert plan is not None and D(plan["notional"]) == D(800)
    assert rulebook.plan_for(CFG, "long", 100.0, 0.001, D(12000)) is None  # below the minimum order


@needs_db
def test_a_stopped_position_sits_out_until_the_signal_has_turned(db: None) -> None:
    seed_all(up=("BTC", "XRP"))
    stopped = NOW - timedelta(hours=6)
    position(
        "trend",
        "trend",
        "BTC",
        "long",
        status="closed",
        close_reason="stop_hard",
        closed_at=stopped,
    )
    position(
        "trend", "trend", "XRP", "long", status="closed", close_reason="signal", closed_at=stopped
    )
    out = rows()
    assert out[("trend", "BTC")].action == "blocked" and out[("trend", "BTC")].plan is None
    assert out[("trend", "BTC")].side == "long"  # it still wants the long; it may not take it
    assert out[("trend", "XRP")].action == "open"  # closed by its signal: free to come back
    with new_session() as s:
        assert rulebook.sits_out(s, "trend", "BTC", "long")
        assert not rulebook.sits_out(s, "trend", "BTC", "short")  # another signal altogether
        # a day on which the book wanted cash: the signal has turned off
        s.add(
            RulePlan(
                ts=NOW + timedelta(days=1),
                day=YESTERDAY + timedelta(days=1),
                book="trend",
                track="trend",
                asset="BTC",
                side="flat",
                action="none",
            )
        )
        s.commit()
        assert not rulebook.sits_out(s, "trend", "BTC", "long")
    # the kill switch and the brake are the owner's call: no sitting out after a resume
    position(
        "short", "short", "ETH", "short", status="closed", close_reason="kill", closed_at=stopped
    )
    with new_session() as s:
        assert not rulebook.sits_out(s, "short", "ETH", "short")


@needs_db
def test_the_challenger_trades_the_days_first_finished_managers_cycle(db: None) -> None:
    seed_all(up=("BTC", "SOL"))
    cycle(TODAY - timedelta(hours=4), {"BTC": "long"})  # yesterday's: not today's call
    cycle(
        TODAY + timedelta(minutes=2),
        {"BTC": "short", "ETH": "flat", "SOL": "long"},
        status="running",
    )
    assert not any(book == "challenger" for book, _ in rows())  # still running at 00:16
    with new_session() as s:
        c = s.scalars(
            select(Cycle).where(Cycle.status == "running", Cycle.trigger == "rulebook-test")
        ).one()
        c.status = "done"
        s.commit()
    cycle(
        TODAY + timedelta(hours=4, minutes=2), {"BTC": "long"}
    )  # a later cycle is not the day's call
    position("challenger", "seats", "SOL", "short")  # it is short SOL; the managers now say long
    out = rows(NOW + timedelta(minutes=30))  # the 00:46 retry picks the finished cycle up
    btc = out[("challenger", "BTC")]
    assert btc.side == "short" and btc.action == "open" and btc.track == "seats"
    assert btc.plan is not None and btc.plan["borrow"] is True and btc.basis is not None
    assert btc.basis["seats"] == [None, None, None] and "cycle_id" in btc.basis
    assert out[("challenger", "ETH")].side == "flat" and out[("challenger", "ETH")].action == "none"
    sol = out[("challenger", "SOL")]
    assert (
        sol.side == "long" and sol.action == "open" and sol.note is not None and "flip" in sol.note
    )
    assert ("challenger", "XRP") not in out  # the managers' cycle did not cover it
    # the rule books were written by the first run and are untouched by the retry
    assert out[("trend", "BTC")].ts == NOW


@needs_db
def test_the_takeover_switch_moves_the_trend_book_to_the_primary_ledger(db: None) -> None:
    seed_all()
    out = rows(cfg=cfg_with(main_book="trend"))
    assert out[("trend", "BTC")].track == "primary"
    assert out[("short", "ETH")].track == "short"
    with new_session() as s:
        assert (
            rulebook.decide(s, get_config().model_copy(update={"strategy": StrategyConfig()}), NOW)
            == []
        )
