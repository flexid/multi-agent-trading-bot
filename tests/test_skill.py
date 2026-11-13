"""Skill against drift (app/decision/skill.py): a call is judged against the same call
at an average moment, so a falling market cannot flatter a short."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import delete

from app.config import get_config
from app.db.models import Candle, Cycle, DecisionRecord
from app.db.session import new_session
from app.decision.skill import Closes, skill_table
from tests.test_data_layer import needs_db

T0 = datetime(2026, 9, 1, tzinfo=UTC)
CFG = get_config()
SYMBOL = CFG.symbol("BTC")


@pytest.fixture
def db() -> Iterator[None]:
    def clean() -> None:
        with new_session() as s:
            s.execute(delete(Cycle).where(Cycle.trigger == "skill-test"))
            s.execute(delete(Candle).where(Candle.interval == "15", Candle.symbol == SYMBOL))
            s.commit()

    clean()
    yield
    clean()


def path(prices: list[tuple[float, float]]) -> None:
    """15-minute closes for BTC: ``prices`` is (hours after T0, price) knots, joined by
    straight lines."""
    with new_session() as s:
        end = int(prices[-1][0] * 4)
        for i in range(end + 1):
            hours = i / 4
            (h0, p0), (h1, p1) = next(
                (a, b) for a, b in zip(prices, prices[1:], strict=False) if a[0] <= hours <= b[0]
            )
            price = p0 + (p1 - p0) * ((hours - h0) / (h1 - h0) if h1 > h0 else 0)
            s.add(
                Candle(
                    symbol=SYMBOL,
                    interval="15",
                    open_time=T0 + timedelta(minutes=15 * i) - timedelta(minutes=15),
                    open=D(str(price)),
                    high=D(str(price)),
                    low=D(str(price)),
                    close=D(str(price)),
                    volume=D(1),
                    turnover=D(1),
                    fetched_at=T0,
                )
            )
        s.commit()


def decide(hours: float, spot: float, consensus: str, pm1: str = "flat", pm2: str = "flat") -> None:
    with new_session() as s:
        ts = T0 + timedelta(hours=hours)
        c = Cycle(
            started_at=ts, kind="scheduled", trigger="skill-test", mode="shadow", status="done"
        )
        s.add(c)
        s.flush()
        s.add(
            DecisionRecord(
                cycle_id=c.id,
                asset="BTC",
                ts=ts,
                spot=D(str(spot)),
                valid_agents=5,
                agreement="agree",
                direction=consensus,
                pm1_direction=pm1,
                pm2_direction=pm2,
                pm3_direction="flat",
                reason="test",
            )
        )
        s.commit()


def test_closes_give_the_last_close_at_or_before_a_time() -> None:
    c = Closes([(T0, 100.0), (T0 + timedelta(minutes=15), 101.0)])
    assert c.at(T0 + timedelta(minutes=14)) is None  # the first candle has not closed yet
    assert c.at(T0 + timedelta(minutes=15)) == 100.0
    assert c.at(T0 + timedelta(minutes=29)) == 100.0
    assert c.at(T0 + timedelta(minutes=30)) == 101.0
    assert c.at(T0 + timedelta(minutes=31)) is None  # beyond the data: no forward return


@needs_db
def test_a_short_in_a_falling_market_has_no_skill_unless_its_timing_is_better(db: None) -> None:
    # flat for a day, then down 2% over the next day, then flat
    path([(0, 100.0), (24, 100.0), (48, 98.0), (120, 98.0)])
    decide(0, 100.0, consensus="flat", pm1="short")  # the next day: nothing happens
    decide(24, 100.0, consensus="short", pm1="short", pm2="long")  # the next day: -2%
    table = {
        s.who.split(" ")[0]: s for s in skill_table(new_session(), CFG, T0 - timedelta(hours=1))
    }
    cons, pm1, pm2 = table["consensus"], table["pm_1"], table["pm_2"]
    # an average moment's short earned 1% over a day (0% and 2%)
    assert cons.calls == 1 and cons.days == 1 and cons.short_share == 1.0
    d1 = cons.horizons["1d"]
    assert d1.n == 1 and d1.mean_pct == pytest.approx(2.0, abs=0.01)
    assert d1.drift_pct == pytest.approx(1.0, abs=0.01) and d1.skill_pct == pytest.approx(
        1.0, abs=0.01
    )
    assert d1.right == 1.0
    # the seat that was short at both moments earned exactly the drift: no skill
    assert pm1.calls == 2 and pm1.days == 2
    assert pm1.horizons["1d"].mean_pct == pytest.approx(1.0, abs=0.01)
    assert pm1.horizons["1d"].skill_pct == pytest.approx(0.0, abs=0.01)
    # the seat that was long into the fall: wrong by more than the drift
    assert pm2.horizons["1d"].mean_pct == pytest.approx(-2.0, abs=0.01)
    assert pm2.horizons["1d"].drift_pct == pytest.approx(-1.0, abs=0.01)
    assert pm2.horizons["1d"].skill_pct == pytest.approx(-1.0, abs=0.01)
    assert pm2.short_share == 0.0 and pm2.horizons["1d"].right == 0.0
    # three days ahead both moments have a price; seven days ahead neither does
    assert cons.horizons["3d"].n == 1 and cons.horizons["7d"].n == 0
    assert cons.horizons["7d"].skill_pct is None
    assert table["pm_3"].calls == 0 and table["pm_3"].short_share is None
    assert cons.as_dict()["1d"]["skill_pct"] == pytest.approx(1.0, abs=0.01)


@needs_db
def test_the_move_that_triggered_the_cycle_is_not_booked_as_the_forecast(db: None) -> None:
    """A triggered cycle runs because the price just moved, and its decision's ``spot`` is
    the last closed four-hour candle: the price before the move. Measured from there, a
    short called after a 2% drop has "earned" 2% the moment it is made. The base is the
    15-minute close at the decision."""
    # 100 until hour 22, down to 98 by hour 24, flat afterwards
    path([(0, 100.0), (22, 100.0), (24, 98.0), (120, 98.0)])
    decide(0, 100.0, consensus="flat")  # an average moment: -2% a day later
    decide(24, 100.0, consensus="short", pm1="short")  # called at 98 with a spot still at 100
    table = {
        s.who.split(" ")[0]: s for s in skill_table(new_session(), CFG, T0 - timedelta(hours=1))
    }
    d1 = table["consensus"].horizons["1d"]
    assert d1.n == 1 and d1.mean_pct == pytest.approx(0.0, abs=0.01)  # nothing after the call
    assert d1.drift_pct == pytest.approx(1.0, abs=0.01)  # an average short earned 1% (2% and 0%)
    assert d1.skill_pct == pytest.approx(-1.0, abs=0.01)  # the call was late, not right
    assert d1.right == 0.0
