"""The DEX mirror on the paper twin with a scripted venue: it follows the main book's
record, closes when the main book closes, and books the venue's own take or stop."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import delete

from app.config import get_config
from app.db.models import Cycle, DecisionRecord, DexEquity, DexState, DexTrade, Position
from app.db.session import new_session
from app.dex.exchange import Instrument, round_price, round_size
from app.dex.mirror import Mirror
from app.dex.paper import PaperDex
from tests.test_data_layer import needs_db

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class Venue:
    def __init__(self) -> None:
        self.px = {"BTC": D(80000), "ETH": D(2400)}

    async def instruments(self) -> dict[str, Instrument]:
        return {"BTC": Instrument("BTC", 5, 40), "ETH": Instrument("ETH", 4, 25)}

    async def mids(self) -> dict[str, D]:
        return dict(self.px)


@pytest.fixture
def db() -> Iterator[None]:
    with new_session() as s:
        for m in (DexEquity, DexTrade, DexState, Position, DecisionRecord, Cycle):
            s.execute(delete(m))
        s.commit()
    yield


def main_position(asset: str, side: str, entry: str, stop: str, target: str, lev: str = "2") -> int:
    with new_session() as s:
        p = Position(
            track="primary",
            mode="paper",
            asset=asset,
            symbol=f"{asset}USDT",
            direction=side,
            status="open",
            qty=D("0.01"),
            entry_price=D(entry),
            notional=D(entry) * D("0.01"),
            margin=D(entry) * D("0.01") / D(lev),
            leverage=D(lev),
            stop=D(stop),
            hard_stop=D(stop),
            target=D(target),
            max_hold_hours=48,
            opened_at=T0,
            order_link_id=f"t-{asset}",
            sleeve="bluechips",
            created_at=T0,
            updated_at=T0,
        )
        s.add(p)
        s.commit()
        return p.id


def close_main(pid: int, reason: str) -> None:
    with new_session() as s:
        p = s.get(Position, pid)
        assert p is not None
        p.status, p.close_reason, p.closed_at, p.pnl_margin_pct = (
            "closed",
            reason,
            T0 + timedelta(hours=1),
            D("0.02"),
        )
        s.commit()


@needs_db
async def test_the_mirror_follows_the_main_book(db: None) -> None:
    venue = Venue()
    ex = PaperDex(equity=D(1000), taker_fee_pct=D("0.035"), prices=venue)
    m = Mirror(get_config(), ex, "paper")
    await m.start(T0)
    await m.tick(T0)
    assert m.start_equity == D(1000)
    pid = main_position("BTC", "short", "80000", "81600", "77600", lev="2")
    now = T0 + timedelta(seconds=5)
    await m.tick(now)
    with new_session() as s:
        (row,) = s.query(DexTrade).all()
    assert row.asset == "BTC" and row.side == "short" and row.leverage == 2 and row.status == "open"
    # 14% of 1000 at 2x = 280 USDT notional at 80000 = 0.0035 BTC
    assert row.qty == D("0.0035") and row.main_position_id == pid
    assert row.sl_price == D(81600) and row.tp_price == D(77600)
    # the main book closes: the mirror closes with its reason
    venue.px["BTC"] = D(79000)
    close_main(pid, "take_profit")
    now += timedelta(seconds=5)
    await m.tick(now)
    with new_session() as s:
        (row,) = s.query(DexTrade).all()
    assert row.status == "closed" and row.exit_reason == "mirror:take_profit"
    assert row.net_roe_pct is not None and row.net_roe_pct > D("2")  # 1.25% × 2x, minus fees
    # a plan mirrored and closed is not reopened while the main position stays closed
    await m.tick(now + timedelta(seconds=5))
    with new_session() as s:
        assert s.query(DexTrade).count() == 1


@needs_db
async def test_the_venue_stop_is_booked_as_sl_and_blocks_nothing(db: None) -> None:
    venue = Venue()
    ex = PaperDex(equity=D(1000), taker_fee_pct=D("0.035"), prices=venue)
    m = Mirror(get_config(), ex, "paper")
    await m.start(T0)
    pid = main_position("ETH", "long", "2400", "2350", "2500", lev="3")
    await m.tick(T0 + timedelta(seconds=5))
    venue.px["ETH"] = D(2340)  # through the stop: the venue's trigger fires
    await m.tick(T0 + timedelta(seconds=10))
    await m.tick(T0 + timedelta(seconds=15))
    with new_session() as s:
        (row,) = s.query(DexTrade).all()
        assert (
            row.status == "closed"
            and row.exit_reason == "sl"
            and row.net_roe_pct is not None
            and row.net_roe_pct < 0
        )
    assert m.mismatch is None
    close_main(pid, "stop")
    await m.tick(T0 + timedelta(seconds=20))
    with new_session() as s:
        assert s.query(DexTrade).count() == 1  # no second mirror of the same main position


def test_venue_rounding() -> None:
    assert round_size(D("0.0035678"), 5) == D("0.00356")
    assert round_price(D("81234.567")) == 81235.0 or round_price(D("81234.567")) == 81234.0
    assert round_price(D("0.0041083")) == 0.004108
