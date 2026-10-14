"""The arena's minute loop on the paper twin with scripted prices and scripted brains: an
entry on a signal, the take and the stop fired by candles, two losses bench, a round
resets when all are benched. No order endpoint is ever touched."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import delete

from app.arena import author, reflex
from app.arena.candles import CandleBuffer
from app.arena.engine import Engine, Trader
from app.arena.paper import PaperExchange
from app.arena.reflex import EarlyExit, Signal
from app.arena.rulebook import Rulebook
from app.config import ArenaConfig, ArenaTraderConfig
from app.db.models import (
    ArenaBenchEvent,
    ArenaDecision,
    ArenaEquity,
    ArenaRound,
    ArenaRulebook,
    ArenaTrade,
    ArenaTrader,
)
from app.db.session import new_session
from tests.test_data_layer import needs_db

T0 = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)


class Prices:
    """A scripted market: a list of 1-minute closes; highs and lows a little around them."""

    def __init__(self) -> None:
        self.closes: list[float] = [60000.0] * 200
        self.minute = 0

    def push(self, close: float, high: float | None = None, low: float | None = None) -> None:
        self.closes.append(close)
        self.minute += 1
        self.hi = high if high is not None else close
        self.lo = low if low is not None else close

    async def klines(self, interval: str, limit: int = 151) -> list[list[float]]:
        step = {"1": 1, "5": 5, "15": 15, "60": 60}[interval]
        rows = []
        for i, c in enumerate(self.closes[-limit:]):
            t = (T0 + timedelta(minutes=(i + self.minute) * step)).timestamp() * 1000
            hi = getattr(self, "hi", c) if i == len(self.closes[-limit:]) - 1 else c
            lo = getattr(self, "lo", c) if i == len(self.closes[-limit:]) - 1 else c
            rows.append([t, c, max(c, hi), min(c, lo), c, 10.0])
        return rows

    async def last_price(self) -> D:
        return D(str(self.closes[-1]))


@pytest.fixture
def db() -> Iterator[None]:
    with new_session() as s:
        for m in (
            ArenaEquity,
            ArenaBenchEvent,
            ArenaTrade,
            ArenaDecision,
            ArenaRulebook,
            ArenaRound,
            ArenaTrader,
        ):
            s.execute(delete(m))
        s.commit()
    yield


RB = Rulebook(
    name="Scripted",
    thesis="A test.",
    leverage=5,
    tp_net_roe_pct=2,
    sl_net_roe_pct=2,
    min_conviction=0.6,
    entry_long="scripted",
    entry_short="scripted",
    no_trade="scripted",
    early_exit="scripted",
)


def make(prices: Prices, *, equity: D = D(1000)) -> tuple[Engine, dict[str, Trader]]:
    cfg = ArenaConfig(
        enabled=True,
        traders={
            k: ArenaTraderConfig(model="test-model") for k in ("kwikton", "kwekton", "kwakton")
        },
    )
    traders = {
        k: Trader(
            id=k,
            model="test-model",
            exchange=PaperExchange(
                "BTCUSDT", equity=equity, taker_fee_pct=D("0.055"), prices=prices
            ),
        )
        for k in cfg.traders
    }
    return Engine(
        cfg, traders, CandleBuffer(traders["kwikton"].exchange), jev_model="jev-test"
    ), traders


@needs_db
async def test_a_round_of_the_arena(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    prices = Prices()
    engine, traders = make(prices)
    signals: dict[str, Signal] = {}

    async def fake_write(model: str, context: str, *, round_number: int) -> Rulebook:
        return RB

    async def fake_entry(
        rb: Rulebook, state: dict[str, object], *, model: str, trader: str
    ) -> Signal:
        return signals.get(trader, Signal("none", {"none": 1.0}, 0.0))

    async def fake_exit(
        rb: Rulebook, state: dict[str, object], *, side: str, model: str, trader: str
    ) -> EarlyExit:
        return EarlyExit(close=False, probability=0.1)

    monkeypatch.setattr(author, "write_rulebook", fake_write)
    monkeypatch.setattr(reflex, "decide_entry", fake_entry)
    monkeypatch.setattr(reflex, "decide_exit", fake_exit)

    now = T0
    await engine.start(now)
    assert engine.round_number == 1 and all(t.plan is not None for t in traders.values())
    prices.push(60000.0)
    await engine.tick(now)  # first tick: equity recorded, traders funded, nothing to act on
    assert all(t.funded for t in traders.values())

    # Kwikton: a long with enough conviction; the others: no signal
    signals["kwikton"] = Signal("long", {"long": 0.8, "short": 0.05, "none": 0.15}, 0.8)
    signals["kwekton"] = Signal(
        "short", {"long": 0.1, "short": 0.5, "none": 0.4}, 0.3
    )  # below the bar
    now += timedelta(minutes=1)
    prices.push(60000.0)
    await engine.tick(now)
    with new_session() as s:
        opens = {x.trader: x for x in s.query(ArenaTrade).filter_by(status="open").all()}
    assert set(opens) == {"kwikton"}
    k = opens["kwikton"]
    assert k.side == "long" and k.leverage == 5 and k.tp_price > k.entry_price > k.sl_price
    # 2% net at 5x with 0.55% fees: gross 2.55% on margin = 0.51% price
    assert float(k.tp_price) == pytest.approx(60000 * 1.0051, rel=1e-4)
    signals.clear()

    # the take fires on a candle whose high crosses it
    now += timedelta(minutes=1)
    prices.push(60200.0, high=60400.0)
    await engine.tick(now)
    with new_session() as s:
        closed = s.query(ArenaTrade).filter_by(trader="kwikton", status="closed").one()
    assert (
        closed.exit_reason == "tp"
        and closed.net_roe_pct is not None
        and closed.net_roe_pct >= D("1.9")
    )
    assert traders["kwikton"].losses == 0

    # two stops in a row bench Kwekton; its third signal is ignored
    for _ in range(3):
        signals["kwekton"] = Signal("long", {"long": 0.9, "short": 0.0, "none": 0.1}, 0.9)
        now += timedelta(minutes=1)
        prices.push(60000.0)
        await engine.tick(now)  # opens (or not, once benched)
        now += timedelta(minutes=1)
        prices.push(59500.0, low=59000.0)
        await engine.tick(now)  # the stop fires
    assert traders["kwekton"].benched and traders["kwekton"].losses == 2
    with new_session() as s:
        assert s.query(ArenaTrade).filter_by(trader="kwekton").count() == 2
        assert s.query(ArenaBenchEvent).filter_by(trader="kwekton").count() == 1
        assert s.query(ArenaDecision).filter_by(trader="kwekton", acted=True).count() == 2

    # bench the other two the same way: the round resets, everyone is back in
    for tid in ("kwikton", "kwakton"):
        for _ in range(2):
            signals[tid] = Signal("short", {"long": 0.0, "short": 0.9, "none": 0.1}, 0.9)
            now += timedelta(minutes=1)
            prices.push(60000.0)
            await engine.tick(now)
            now += timedelta(minutes=1)
            prices.push(60500.0, high=61000.0)
            await engine.tick(now)
        signals.pop(tid, None)
    assert engine.round_number == 2
    assert all(not t.benched and t.losses == 0 for t in traders.values())
    with new_session() as s:
        rounds = s.query(ArenaRound).order_by(ArenaRound.number).all()
        assert rounds[0].ended_at is not None and rounds[0].winner == "kwikton"
        assert s.query(ArenaRulebook).filter_by(round=2).count() == 3
        assert s.query(ArenaEquity).filter_by(trader="kwekton").count() > 5


def test_paper_exchange_books_fees_both_ways() -> None:
    import asyncio

    prices = Prices()
    ex = PaperExchange("BTCUSDT", equity=D(1000), taker_fee_pct=D("0.055"), prices=prices)

    async def run() -> None:
        await ex.set_leverage(10)
        await ex.open_market("long", D("0.1"), D(60300), D(59700), "t1")
        eq, _ = await ex.equity()
        assert eq < D(1000)  # the entry fee is paid at once
        await ex.on_candle(D(60300), D(60000))
        (t,) = await ex.closed_trades(datetime(2000, 1, 1, tzinfo=UTC))
        assert t.exit == D(60300) and t.fees == D("0.1") * (D(60000) + D(60300)) * D("0.00055")
        assert t.pnl == D("0.1") * D(300) - t.fees

    asyncio.run(run())


@needs_db
async def test_a_restart_writes_the_rulebooks_the_round_still_lacks(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart while the authors were writing (2026-10-13, first deploy) left the round
    without rulebooks and the traders idle. On start, traders without a book get one."""
    prices = Prices()
    written: list[str] = []

    async def fake_write(model: str, context: str, *, round_number: int) -> Rulebook:
        written.append(model)
        return RB

    monkeypatch.setattr(author, "write_rulebook", fake_write)
    with new_session() as s:
        s.add(ArenaRound(number=1, started_at=T0))
        s.commit()
    engine, traders = make(prices)
    await engine.start(T0)
    assert engine.round_number == 1 and len(written) == 3
    assert all(t.plan is not None for t in traders.values())
    # a second start finds the books and writes nothing
    written.clear()
    engine2, _ = make(prices)
    await engine2.start(T0)
    assert written == []


@needs_db
async def test_a_round_also_ends_on_the_time_limit(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner 2026-10-14: a trader that never trades can never be benched; after
    max_round_hours the round resets anyway, any open position closed first."""
    prices = Prices()
    engine, traders = make(prices)
    engine.cfg = engine.cfg.model_copy(update={"max_round_hours": 2})

    async def fake_write(model: str, context: str, *, round_number: int) -> Rulebook:
        return RB

    async def fake_entry(
        rb: Rulebook, state: dict[str, object], *, model: str, trader: str
    ) -> Signal:
        return Signal("none", {"none": 1.0}, 0.0)

    monkeypatch.setattr(author, "write_rulebook", fake_write)
    monkeypatch.setattr(reflex, "decide_entry", fake_entry)
    now = T0
    await engine.start(now)
    prices.push(60000.0)
    await engine.tick(now)
    assert engine.round_number == 1
    now = T0 + timedelta(hours=1, minutes=59)
    prices.push(60000.0)
    await engine.tick(now)
    assert engine.round_number == 1
    now = T0 + timedelta(hours=2)
    prices.push(60000.0)
    await engine.tick(now)
    assert engine.round_number == 2
    with new_session() as s:
        first = s.query(ArenaRound).filter_by(number=1).one()
        assert first.ended_at is not None


@needs_db
async def test_structured_rules_trade_without_jev(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-15: a rulebook with conditions is evaluated in code; Jev is not asked. A
    5m close above the prior eight highs opens a long; a no-trade condition blocks it."""
    from app.arena.conditions import Condition, Ref

    prices = Prices()
    engine, traders = make(prices)
    rb = RB.model_copy(
        update={
            "long_conditions": [
                Condition(
                    timeframe="5m",
                    field="close",
                    op=">",
                    ref=Ref(field="high", agg="max", lookback=8, index=1),
                )
            ],
            "short_conditions": [
                Condition(
                    timeframe="5m",
                    field="close",
                    op="<",
                    ref=Ref(field="low", agg="min", lookback=8, index=1),
                )
            ],
            "no_trade_conditions": [
                Condition(
                    timeframe="1m",
                    field="range",
                    op=">",
                    ref=Ref(field="close", index=0, scale=0.01),
                )
            ],
        }
    )
    asked: list[str] = []

    async def fake_write(model: str, context: str, *, round_number: int) -> Rulebook:
        return rb

    async def no_jev(*a: object, **k: object) -> None:
        asked.append("jev")
        return None

    monkeypatch.setattr(author, "write_rulebook", fake_write)
    monkeypatch.setattr(reflex, "decide_entry", no_jev)
    now = T0
    await engine.start(now)
    prices.push(60000.0)
    await engine.tick(now)
    for _ in range(3):  # flat: no entry
        now += timedelta(minutes=1)
        prices.push(60000.0)
        await engine.tick(now)
    with new_session() as s:
        assert s.query(ArenaTrade).count() == 0
    # a close 1% above everything before it: all three open longs
    now += timedelta(minutes=1)
    prices.push(60600.0)
    await engine.tick(now)
    with new_session() as s:
        opens = s.query(ArenaTrade).filter_by(status="open").all()
        assert {x.trader for x in opens} == set(traders) and all(x.side == "long" for x in opens)
        d = s.query(ArenaDecision).filter_by(acted=True).first()
        assert d is not None and d.probabilities["long"] == 1.0 and d.conviction == 1.0
    assert asked == []  # Jev was never consulted
