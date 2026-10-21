"""v13: the managers' pack carries the open position per asset and the book's totals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.agents.schema import AgentOutput, Horizon
from app.db.models import Candle, Position
from app.db.session import new_session
from app.decision.evidence import build_pack, net_margin_pct, open_positions
from tests.test_data_layer import needs_db

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _pos(asset: str, direction: str, entry: str, **kw: object) -> Position:
    base: dict[str, object] = dict(
        mode="paper",
        track="primary",
        status="open",
        symbol=f"{asset}USDT",
        direction=direction,
        qty=D(100) / D(entry),  # notional 100 at the entry
        entry_price=D(entry),
        leverage=D(2),
        margin=D(50),
        notional=D(100),
        stop=D(entry) * (D("0.98") if direction == "long" else D("1.02")),
        target=D(entry) * (D("1.04") if direction == "long" else D("0.96")),
        fees=D("0.1"),
        max_hold_hours=48,
        opened_at=NOW - timedelta(hours=3),
        created_at=NOW - timedelta(hours=3),
        updated_at=NOW - timedelta(hours=3),
    )
    base.update(kw)
    return Position(asset=asset, **base)  # type: ignore[arg-type]


def test_net_margin_pct_is_net_of_fees() -> None:
    p = _pos("ETH", "long", "10")
    # +2% on 100 notional = +2, minus 0.1 entry fee and 0.102 exit fee, on 50 margin
    assert net_margin_pct(p, 10.2) == pytest.approx((2 - 0.1 - 0.102) / 50)
    assert net_margin_pct(_pos("ETH", "short", "10"), 10.2) == pytest.approx(
        (-2 - 0.1 - 0.102) / 50
    )
    assert net_margin_pct(p, 0.0) == 0.0


@needs_db
def test_pack_carries_positions_and_book() -> None:
    with new_session() as s:
        s.query(Position).filter(Position.asset.in_(("BTC", "ETH", "SOL"))).delete()
        s.query(Candle).filter(Candle.symbol.in_(("BTCUSDT", "ETHUSDT"))).delete()
        for sym, close in (("BTCUSDT", "102"), ("ETHUSDT", "9.8")):
            s.add(
                Candle(
                    symbol=sym,
                    interval="240",
                    open_time=NOW - timedelta(hours=4),
                    open=D(close),
                    high=D(close),
                    low=D(close),
                    close=D(close),
                    volume=D(1),
                    turnover=D(1),
                    fetched_at=NOW,
                )
            )
        s.add_all(
            [
                _pos("BTC", "long", "100"),
                _pos("ETH", "short", "10"),
                _pos("SOL", "long", "100", status="closed"),  # not open: not in the book
                _pos("SOL", "long", "100", track="max"),  # another book
            ]
        )
        s.commit()
    with new_session() as s:
        held, book = open_positions(s, "paper", NOW, {"BTC": 102.0, "ETH": 9.8})
        assert set(held) == {"BTC", "ETH"}
        assert held["BTC"].direction == "long" and held["BTC"].hours_open == 3.0
        assert held["BTC"].net_pct == pytest.approx(round((2 - 0.1 - 0.102) / 50 * 100, 2))
        assert book.positions == 2 and book.long == ["BTC"] and book.short == ["ETH"]
        assert book.net_pct > 0

        outs = {
            a: [
                AgentOutput(
                    agent="indicators",
                    asset=a,
                    score=0.5,
                    confidence=0.6,
                    horizon=Horizon.D1,
                    evidence=["x"],
                    data_age_min=1,
                )
            ]
            for a in ("BTC", "ETH")
        }
        pack = build_pack(
            s,
            mode="shadow",  # the cycle's mode; the paper book's positions carry "paper"
            outputs=outs,
            symbols={"BTC": "BTCUSDT", "ETH": "ETHUSDT"},
            macro_regime=None,
            couplings={},
            now=NOW,
        )
        by = {a.asset: a for a in pack.assets}
        assert by["BTC"].open_position is not None and by["BTC"].open_position.leverage == 2.0
        assert by["ETH"].open_position is not None and by["ETH"].open_position.direction == "short"
        assert pack.book.long == ["BTC"] and pack.book.short == ["ETH"]
        dumped = pack.model_dump()
        assert dumped["book"]["positions"] == 2
