"""M9 fix 1 and 2 on the live gateway path: exits are never post-only, they are retried
until the position is flat, and every real position has a stop resting on the exchange.

The gateway tests need nothing but the scripted exchange in ``tests/fake_bybit.py``. The
executor tests run against the test Postgres (skipped when it is down). No network, no
real orders.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.config import get_config, get_secrets
from app.db.models import Position, XPostOut
from app.execution import bybit_gateway
from app.execution.bybit_gateway import BybitGateway
from app.execution.executor import Executor
from app.execution.gateway import OrderOutcome, Outcome, ScriptedGateway
from app.execution.simulator import Quote, Side
from tests.fake_bybit import FakeBybit
from tests.test_data_layer import needs_db
from tests.test_executor_failures import (  # noqa: F401  (db is a fixture)
    ASSET,
    db,
    make,
    positions,
    quote,
    seed_decision,
)

NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(
    minutes=5
)  # the executor only opens decisions younger than one cycle
SYMBOL = "BTCUSDT"


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bybit_gateway, "POLL_S", 0)
    monkeypatch.setattr(bybit_gateway, "SETTLE_POLL_S", 0)


def q(bid: str, ask: str) -> Quote:
    return Quote(D(bid), D(ask), NOW)


# --- gateway ------------------------------------------------------------------------------


async def test_entries_stay_post_only() -> None:
    ex = FakeBybit(SYMBOL, "99.9", "100")
    async with ex.client() as client:
        r = await BybitGateway(client).open(
            SYMBOL, Side.LONG, D("0.4"), False, "live-a", q("99.9", "100")
        )
    assert r.outcome is Outcome.FILLED
    assert [o["timeInForce"] for o in ex.created] == ["PostOnly"]


async def test_exit_is_ioc_priced_half_the_band_beyond_the_touch_never_post_only() -> None:
    ex = FakeBybit(SYMBOL, "90", "90.1")
    async with ex.client() as client:
        gw = BybitGateway(client)
        sold = await gw.close(SYMBOL, Side.LONG, D("0.4"), "live-a-x", q("99.9", "100"))
        bought = await gw.close(SYMBOL, Side.SHORT, D("0.4"), "live-b-x", q("99.9", "100"))
    assert sold.outcome is Outcome.FILLED and sold.avg_price == D(90)  # the real, gapped price
    assert bought.outcome is Outcome.FILLED and bought.avg_price == D("90.1")
    sell, buy = ex.created
    assert sell["timeInForce"] == buy["timeInForce"] == "IOC"
    assert D(sell["price"]) == D("89.5")  # half the 1% band below the bid it actually saw
    assert D(buy["price"]) == D("90.6")  # half the 1% band above the ask, rounded up to the tick
    assert sell["orderLinkId"] != buy["orderLinkId"] and sell["orderLinkId"].startswith("live-a-x")


async def test_exit_into_a_thin_book_is_resent_until_flat() -> None:
    ex = FakeBybit(SYMBOL, "90", "90.1")
    ex.ioc_liquidity = [D("0.1"), D("0.1")]  # two thin levels, then enough
    async with ex.client() as client:
        r = await BybitGateway(client).close(
            SYMBOL, Side.LONG, D("0.4"), "live-a-x", q("90", "90.1")
        )
    assert r.outcome is Outcome.FILLED and r.filled_qty == D("0.4")
    assert [D(o["qty"]) for o in ex.created] == [D("0.4"), D("0.3"), D("0.2")]
    assert len({o["orderLinkId"] for o in ex.created}) == 3  # every attempt has its own id


async def test_exit_that_cannot_fill_reports_partial_so_the_executor_calls_again() -> None:
    ex = FakeBybit(SYMBOL, "90", "90.1")
    ex.ioc_liquidity = [D("0.05")] * bybit_gateway.EXIT_ATTEMPTS
    async with ex.client() as client:
        r = await BybitGateway(client).close(
            SYMBOL, Side.LONG, D("0.4"), "live-a-x", q("90", "90.1")
        )
    assert r.outcome is Outcome.PARTIAL and r.filled_qty == D("0.25")
    assert len(ex.created) == bybit_gateway.EXIT_ATTEMPTS


async def test_exit_whose_reply_was_lost_is_settled_before_anything_else_is_sent() -> None:
    ex = FakeBybit(SYMBOL, "90", "90.1")
    ex.drop_next_create = True  # the sell reaches the exchange and fills; we never hear back
    async with ex.client() as client:
        gw = BybitGateway(client)
        first = await gw.close(SYMBOL, Side.LONG, D("0.4"), "live-a-x", q("90", "90.1"))
        assert first.outcome is Outcome.UNREACHABLE and first.filled_qty == 0
        second = await gw.close(SYMBOL, Side.LONG, D("0.4"), "live-a-x", q("90", "90.1"))
    assert second.outcome is Outcome.FILLED and second.filled_qty == D("0.4")
    assert len(ex.created) == 1  # the retry found the fill; it did not sell twice


async def test_backup_stop_is_a_conditional_market_order_without_leverage() -> None:
    ex = FakeBybit(SYMBOL, "99.9", "100")
    async with ex.client() as client:
        gw = BybitGateway(client)
        assert await gw.place_backup_stop(SYMBOL, Side.LONG, D("0.4"), D("96.97"), "bk-1")
        assert await gw.place_backup_stop(SYMBOL, Side.SHORT, D("0.4"), D("103.03"), "bk-2")
        assert await gw.backup_stop_fill(SYMBOL, "bk-1") is None  # resting
        ex.trigger_stop("bk-1", "96.5")
        fill = await gw.backup_stop_fill(SYMBOL, "bk-1")
        assert await gw.cancel_backup_stop(SYMBOL, "bk-2")
        assert await gw.cancel_backup_stop(SYMBOL, "bk-2")  # already gone counts as gone
    long_stop, short_stop = ex.created
    assert long_stop["orderFilter"] == "StopOrder" and long_stop["orderType"] == "Market"
    assert long_stop["side"] == "Sell" and long_stop["isLeverage"] == 0
    assert D(long_stop["triggerPrice"]) == D("96.9")  # rounded away from the market
    assert short_stop["side"] == "Buy" and D(short_stop["triggerPrice"]) == D("103.1")
    assert short_stop["marketUnit"] == "baseCoin" and "timeInForce" not in short_stop
    assert fill is not None and fill.outcome is Outcome.FILLED and fill.avg_price == D("96.5")


# --- executor on the live gateway ---------------------------------------------------------


def pilot(ex: FakeBybit) -> Executor:
    """An executor in pilot mode: paper tracks on the paper book, the pilot on ``ex``."""
    executor = Executor(get_config(), get_secrets(), BybitGateway(ex.client()), feed=False)
    executor.mode = "pilot"
    executor.posting_live = lambda: False  # type: ignore[method-assign]
    executor._alert = lambda subject, body: None  # type: ignore[method-assign]
    symbol = get_config().symbol(ASSET)
    executor.quotes[symbol] = quote("99.9", "100")
    executor.qty_steps[symbol] = D("0.001")
    return executor


def set_quote(executor: Executor, ex: FakeBybit, bid: str, ask: str) -> None:
    ex.move(bid, ask)
    executor.quotes[get_config().symbol(ASSET)] = quote(bid, ask)


@needs_db
async def test_price_gaps_through_a_stop_on_the_live_gateway(db: None) -> None:  # noqa: F811
    seed_decision(stop="98", atr="2")
    ex = FakeBybit(get_config().symbol(ASSET), "99.9", "100")
    executor = pilot(ex)
    await executor.tick()
    (pos,) = positions("pilot")
    assert pos.mode == "pilot" and pos.status == "open"
    assert pos.backup_stop_price == D(97)  # stop 98 minus 0.5 × ATR 2
    (stop,) = ex.resting_stops()
    assert stop["orderLinkId"] == pos.backup_stop_link and D(stop["triggerPrice"]) == D(97)

    set_quote(executor, ex, "90", "90.1")  # gapped 8% through the stop and the backup
    await executor.tick()
    (pos,) = positions("pilot")
    assert pos.status == "closed" and pos.close_reason == "stop"
    assert pos.exit_price == D(90) and pos.exit_price < pos.stop  # the real fill, not the stop
    assert pos.backup_stop_link is None and ex.resting_stops() == []
    exits = [o for o in ex.created if o["side"] == "Sell" and "timeInForce" in o]
    assert [o["timeInForce"] for o in exits] == ["IOC"] and D(exits[0]["price"]) == D("89.5")
    assert not any(o.get("timeInForce") == "PostOnly" and o["side"] == "Sell" for o in ex.created)
    # The backup came off the book before the bot's own order went out.
    assert ex.calls.index(f"cancel:{stop['orderLinkId']}") < ex.calls.index("create:IOC")


@needs_db
async def test_pilot_runs_beside_the_paper_tracks_and_is_never_posted(db: None) -> None:  # noqa: F811
    from app.db.session import new_session

    seed_decision(stop="98", atr="2")
    ex = FakeBybit(get_config().symbol(ASSET), "99.9", "100")
    executor = pilot(ex)
    await executor.tick()
    (primary,), (pilot_pos,) = positions("primary"), positions("pilot")
    assert primary.mode == "paper" and primary.backup_stop_link is None  # paper never rests a stop
    assert len(ex.created) == 2  # the pilot's entry and its backup stop; nothing for paper
    set_quote(executor, ex, "97", "97.1")
    await executor.tick()
    assert positions("primary")[0].status == positions("pilot")[0].status == "closed"
    with new_session() as s:
        posted = set(s.scalars(select(XPostOut.position_id)).all())
    assert primary.id in posted and pilot_pos.id not in posted


@needs_db
async def test_backup_stop_follows_the_trailing_stop(db: None) -> None:  # noqa: F811
    seed_decision(stop="98", target="110", atr="2")
    ex = FakeBybit(get_config().symbol(ASSET), "99.9", "100")
    executor = pilot(ex)
    await executor.tick()
    first = positions("pilot")[0].backup_stop_link
    # Filled at the bid (99.9), so R = 1.9. At 103 the trail arms at 103 − 1.9 = 101.1.
    set_quote(executor, ex, "103", "103.1")
    await executor.tick()
    (pos,) = positions("pilot")
    assert pos.trail_stop == D("101.1") and pos.backup_stop_price == D("100.1")
    assert pos.backup_stop_link != first
    assert [D(s["triggerPrice"]) for s in ex.resting_stops()] == [D("100.1")]  # one stop, moved


@needs_db
async def test_exchange_triggered_backup_stop_is_booked_and_not_sold_again(db: None) -> None:  # noqa: F811
    seed_decision(stop="98", atr="2")
    ex = FakeBybit(get_config().symbol(ASSET), "99.9", "100")
    executor = pilot(ex)
    await executor.tick()
    link = positions("pilot")[0].backup_stop_link
    assert link is not None
    ex.trigger_stop(link, "96.8")  # the exchange acted while the bot was away
    set_quote(executor, ex, "99", "99.1")  # and the price is back above the bot's stop
    await executor.tick()
    (pos,) = positions("pilot")
    assert pos.status == "closed" and pos.close_reason == "stop" and pos.exit_price == D("96.8")
    assert "create:IOC" not in ex.calls


@needs_db
async def test_exit_waits_while_the_backup_stop_cannot_be_cancelled(db: None) -> None:  # noqa: F811
    seed_decision(stop="98", atr="2")
    ex = FakeBybit(get_config().symbol(ASSET), "99.9", "100")
    executor = pilot(ex)
    await executor.tick()
    ex.fail_cancel = True
    set_quote(executor, ex, "97.5", "97.6")
    await executor.tick()
    (pos,) = positions("pilot")
    assert pos.status == "closing" and "create:IOC" not in ex.calls  # one stop at a time
    ex.fail_cancel = False
    await executor.tick()
    assert positions("pilot")[0].status == "closed"


# --- executor bookkeeping, any gateway ----------------------------------------------------


@needs_db
async def test_rejected_exit_is_not_booked_as_closed(db: None) -> None:  # noqa: F811
    seed_decision()
    gw = ScriptedGateway(
        opens=[OrderOutcome(Outcome.FILLED)],
        closes=[
            OrderOutcome(Outcome.FILLED),  # the comparison track
            OrderOutcome(Outcome.REJECTED, detail="nothing filled"),
            OrderOutcome(Outcome.FILLED),
        ],
    )
    executor = make(gw, quote("99.9", "100"))
    await executor.tick()
    executor.quotes[get_config().symbol(ASSET)] = quote("97", "97.1")
    await executor.tick()
    by_status = {p.status for p in positions("primary") + positions("max")}
    assert by_status == {"closed", "closing"}  # one landed, the refused one is still open
    await executor.tick()
    assert {p.status for p in positions("primary") + positions("max")} == {"closed"}


@needs_db
async def test_partial_exit_keeps_closing_and_books_once_at_the_average(db: None) -> None:  # noqa: F811
    seed_decision()
    gw = ScriptedGateway(
        opens=[OrderOutcome(Outcome.FILLED)],
        closes=[
            OrderOutcome(Outcome.PARTIAL, filled_qty=D(5), avg_price=D(97)),
            OrderOutcome(Outcome.FILLED, avg_price=D(95)),
        ],
    )
    executor = make(gw, quote("99.9", "100"))
    await executor.tick()
    executor.quotes[get_config().symbol(ASSET)] = quote("97", "97.1")
    await executor.tick()
    first = next(p for p in positions("primary") + positions("max") if p.status == "closing")
    assert first.exit_filled_qty == D(5) and first.pnl is None  # nothing booked yet
    await executor.tick()
    from app.db.session import new_session

    with new_session() as s:
        done = s.get(Position, first.id)
    assert done is not None and done.status == "closed" and done.close_reason == "stop"
    expected = (D(5) * D(97) + (done.qty - D(5)) * D(95)) / done.qty
    assert done.exit_price == pytest.approx(expected)


async def test_long_exit_sells_what_is_held_and_cover_buys_the_fee_back() -> None:
    """Spot fees come out of the coin received: the exit must not try to sell more than
    the wallet holds, and a short cover must buy slightly more than it owes."""
    from app.execution.bybit_gateway import BybitGateway
    from app.execution.simulator import Side

    fake = FakeBybit("BTCUSDT", "100", "100.1", tick="0.1")
    fake.holdings["BTC"] = D("0.0999")  # the fee came out of the 0.1 bought
    gw = BybitGateway(fake.client())
    r = await gw.close("BTCUSDT", Side.LONG, D("0.1"), "x-", q("100", "100.1"))
    assert r.filled_qty == D("0.099")  # held 0.0999, lot step 0.001
    fake2 = FakeBybit("BTCUSDT", "100", "100.1", tick="0.1")
    gw2 = BybitGateway(fake2.client())
    r2 = await gw2.close("BTCUSDT", Side.SHORT, D("0.1"), "y-", q("100", "100.1"))
    assert r2.filled_qty > D("0.1")
    assert fake2.repaid == ["BTC"]  # the buy-back does not clear the borrow by itself
    assert fake.repaid == []


@needs_db
async def test_live_mode_books_on_its_own_ledger_seeded_from_real_equity(db: None) -> None:  # noqa: F811
    """Go-live must not inherit the paper history: the live track gets ledger 4 at the
    subaccount's real equity, and the risk engine's starting capital is that number."""
    from datetime import UTC, datetime

    from sqlalchemy import delete

    from app.db.models import AccountSnapshot, PaperAccount, RiskState
    from app.db.session import new_session

    seed_decision(stop="98", atr="2")
    with new_session() as s:
        s.execute(delete(AccountSnapshot))
        s.add(
            AccountSnapshot(
                ts=datetime.now(UTC),
                total_equity=D("1234.5"),
                quote_balance=D("1234.5"),
                coins=[],
                open_orders=[],
            )
        )
        state = s.get(RiskState, 1)
        if state is not None:
            state.starting_capital = None
        s.commit()
    ex = FakeBybit(get_config().symbol(ASSET), "99.9", "100")
    executor = pilot(ex)
    executor.mode = "live"
    await executor.tick()
    (pos,) = positions("live")
    assert pos.mode == "live"
    # the paper tracks keep running beside live as the control group; no pilot in live
    assert len(positions("primary")) == 1 and len(positions("max")) == 1
    assert positions("primary")[0].mode == "paper" and positions("pilot") == []
    with new_session() as s:
        live = s.get(PaperAccount, 4)
        assert live is not None and live.track == "live" and live.starting_capital == D("1234.5")
        state = s.get(RiskState, 1)
        assert state is None or state.starting_capital == D("1234.5")
        s.execute(delete(AccountSnapshot))
        s.commit()
