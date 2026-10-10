"""Logic v4: two sleeves of one book (owner 2026-10-10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from app.config import get_config
from app.decision.pm import Direction
from app.risk import engine
from app.risk.exposure import beta_from_returns, correlation
from app.risk.sleeves import day_locked, lock_until_midnight
from tests.test_data_layer import needs_db
from tests.test_risk import account, cons, limits, market

NOW = datetime(2026, 10, 10, 14, 30, tzinfo=UTC)


def test_config_universe_is_the_union_of_the_sleeves() -> None:
    cfg = get_config()
    assert set(cfg.sleeves) == {"bluechips", "alts"}
    assert cfg.trading.assets == [*cfg.sleeves["bluechips"].assets, *cfg.sleeves["alts"].assets]
    assert cfg.sleeve_of("BTC") == "bluechips" and cfg.sleeve_of("PEPE") == "alts"
    assert cfg.sleeve_of("BNB") is None and cfg.sleeve_cfg("BNB") is None
    alts, blue = cfg.sleeves["alts"], cfg.sleeves["bluechips"]
    assert alts.capital_fraction + blue.capital_fraction == D(1)
    assert alts.leverage_max <= D(3) and alts.risk_per_trade <= blue.risk_per_trade
    assert alts.weights.get("polymarket") == 0.0


def test_cross_sleeve_cap_limits_same_direction_exposure_of_the_whole_book() -> None:
    # the sleeve itself has room (net 0), but the book as a whole is 1.2× long at 1.5× cap
    acct = account(net_beta_exposure=D(0), total_equity=D(10000), total_net_beta_exposure=D(12000))
    a = engine.assess(cons(), acct, market(), limits())
    assert a.allowed and a.plan is not None and a.plan.notional <= D(3000)
    assert any(h.rule == "correlated_exposure_total" for h in a.hits)
    # a short against the book's net is not capped by it
    s = engine.assess(
        cons(direction=Direction.SHORT, stop=103.0, target=94.0), acct, market(), limits()
    )
    assert not any(h.rule == "correlated_exposure_total" for h in s.hits)
    # at the cap nothing fits
    full = engine.assess(
        cons(),
        account(net_beta_exposure=D(0), total_equity=D(10000), total_net_beta_exposure=D(15000)),
        market(),
        limits(),
    )
    assert not full.allowed


def test_day_lock_holds_until_midnight_utc() -> None:
    until = lock_until_midnight(NOW)
    assert until == "2026-10-11T00:00:00+00:00"
    locks = {"alts": until}
    assert day_locked(locks, "alts", NOW)
    assert not day_locked(locks, "bluechips", NOW)
    assert not day_locked(locks, "alts", NOW + timedelta(hours=10))
    assert not day_locked(None, "alts", NOW)


def test_correlation_and_beta_on_4h_returns() -> None:
    btc = [0.01, -0.02, 0.03, -0.01, 0.02, -0.03, 0.01, 0.02, -0.02, 0.01, 0.03, -0.01]
    assert correlation([x * 2 for x in btc], btc) == 1.0
    assert correlation([-x for x in btc], btc) == -1.0
    assert correlation(btc[:5], btc[:5]) is None  # too few points
    assert correlation([0.0] * 12, btc) is None  # no variance
    assert beta_from_returns([x * 2 for x in btc], btc) == D(2)


def llm_call(task: str, asset: str | None, usd: str) -> object:
    from app.db.models import LLMCall

    return LLMCall(
        ts=NOW,
        task=task,
        asset=asset,
        provider="anthropic",
        model="m",
        prompt_name=task,
        prompt_version=1,
        input_chars=1,
        input_tokens=1,
        output_tokens=1,
        cost_usd=D(usd),
        latency_ms=1,
        attempts=1,
        ok=True,
    )


@needs_db
def test_sleeve_account_and_costs_are_computed_per_sleeve() -> None:
    from sqlalchemy import delete

    from app.costs_sleeves import costs_by_sleeve
    from app.db.models import Candle, LLMCall, PaperAccount, Position, XPostRecord
    from app.db.session import new_session
    from app.risk.sleeves import sleeve_account

    cfg = get_config()
    with new_session() as s:
        for table in (Position, Candle, LLMCall, XPostRecord):
            s.execute(delete(table))
        acct = s.get(PaperAccount, 1)
        if acct is None:
            acct = PaperAccount(
                id=1,
                track="primary",
                starting_capital=D(10000),
                cash=D(10000),
                equity=D(10000),
                day_start_equity=D(10000),
                day_date=NOW,
                day_high_equity=D(10000),
                updated_at=NOW,
            )
            s.add(acct)
        acct.starting_capital = D(10000)
        s.add(
            Candle(
                symbol=cfg.symbol("PEPE"),
                interval="15",
                open_time=NOW - timedelta(minutes=15),
                open=D("0.0000100"),
                high=D("0.0000110"),
                low=D("0.0000090"),
                close=D("0.0000110"),
                volume=D(1),
                turnover=D(1),
                fetched_at=NOW,
            )
        )
        base = dict(
            mode="paper",
            track="primary",
            leverage=D(2),
            stop=D(1),
            target=D(2),
            max_hold_hours=24,
            fees=D(0),
            interest=D(0),
            created_at=NOW,
            updated_at=NOW,
        )
        s.add_all(
            [
                # alts: one closed win today, one open long now 10% up in price
                Position(
                    asset="PEPE",
                    symbol=cfg.symbol("PEPE"),
                    direction="long",
                    sleeve="alts",
                    status="closed",
                    qty=D(1),
                    entry_price=D(1),
                    exit_price=D(2),
                    margin=D(50),
                    notional=D(100),
                    pnl=D(30),
                    opened_at=NOW - timedelta(hours=5),
                    closed_at=NOW - timedelta(hours=1),
                    **base,
                ),
                Position(
                    asset="PEPE",
                    symbol=cfg.symbol("PEPE"),
                    direction="long",
                    sleeve="alts",
                    status="open",
                    qty=D(10_000_000),
                    entry_price=D("0.0000100"),
                    margin=D(50),
                    notional=D(100),
                    opened_at=NOW - timedelta(hours=2),
                    **base,
                ),
                # bluechips: one closed loss yesterday
                Position(
                    asset="BTC",
                    symbol=cfg.symbol("BTC"),
                    direction="short",
                    sleeve="bluechips",
                    status="closed",
                    qty=D(1),
                    entry_price=D(1),
                    exit_price=D(2),
                    margin=D(500),
                    notional=D(1000),
                    pnl=D(-40),
                    opened_at=NOW - timedelta(days=2),
                    closed_at=NOW - timedelta(days=1),
                    **base,
                ),
            ]
        )
        s.add_all(
            [
                llm_call("pm", "PEPE", "0.25"),
                llm_call("pm", "BTC", "1.5"),
                llm_call("macro", None, "0.1"),
                XPostRecord(id="1", fetched_at=NOW, query_asset="PEPE", text="x"),
                XPostRecord(id="2", fetched_at=NOW, query_asset="PEPE", text="y"),
                XPostRecord(id="3", fetched_at=NOW, query_asset="ETH", text="z"),
            ]
        )
        s.commit()
        beta = {a: D(1) for a in cfg.trading.assets}
        alts = sleeve_account(s, cfg, "alts", "primary", NOW, beta, 1)
        blue = sleeve_account(s, cfg, "bluechips", "primary", NOW, beta, 1)
        assert alts.capital == D(3000) and blue.capital == D(7000)
        # alts: +30 realized today, +10 unrealized (10M × 0.000001) → equity 3040, day +1.33%
        assert alts.equity == D(3040) and alts.open_positions == 1
        assert alts.gross_exposure == D(100) and alts.net_beta_exposure == D(100)
        assert round(alts.day_pnl_pct, 4) == D("0.0133")
        # bluechips: yesterday's loss counts in equity, not in today's P&L
        assert blue.equity == D(6960) and blue.day_pnl_pct == 0 and blue.open_positions == 0
        costs = {c.sleeve: c for c in costs_by_sleeve(s, cfg, NOW - timedelta(hours=1))}
        assert costs["alts"].llm_usd == D("0.25") and costs["alts"].x_reads == 2
        assert costs["bluechips"].llm_usd == D("1.5") and costs["bluechips"].x_reads == 1
        assert costs["shared"].llm_usd == D("0.1") and costs["shared"].x_reads == 0
        s.execute(delete(Position))
        s.execute(delete(LLMCall))
        s.execute(delete(XPostRecord))
        s.commit()
