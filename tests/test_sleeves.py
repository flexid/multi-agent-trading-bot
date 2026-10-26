"""Logic v4: two sleeves of one book (owner 2026-10-10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from app.config import get_config
from app.decision.pm import Direction
from app.risk import engine
from app.risk.exposure import beta_from_returns, correlation
from app.risk.sleeves import (
    day_locked,
    directions_to_lock,
    lock_info,
    lock_until_midnight,
    losing_directions,
    new_lock,
)
from tests.test_data_layer import needs_db
from tests.test_risk import account, cons, limits, market

NOW = datetime(2026, 10, 10, 14, 30, tzinfo=UTC)


def test_config_is_one_book_of_six() -> None:
    """v14 (owner 2026-10-26): the sleeves are gone; six assets in one pool."""
    cfg = get_config()
    assert cfg.sleeves == {}
    assert cfg.trading.assets == ["BTC", "ETH", "SOL", "XRP", "DOGE", "ENA"]
    assert cfg.sleeve_of("BTC") is None and cfg.sleeve_cfg("ENA") is None
    assert cfg.trading.leverage_max == 3 and cfg.trading.max_hold_hours == 48
    assert cfg.agents.weights_by_asset["ENA"]["polymarket"] == 0.0


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
def test_the_day_stop_locks_only_the_direction_that_lost() -> None:
    """Logic v8 (owner 2026-10-11): the sleeve is closed, new trades are locked only in the
    losing direction, so the bot can follow a changed view the other way."""
    assert losing_directions({"short": D(-158), "long": D(0)}) == {"short"}
    assert losing_directions({"short": D(-90), "long": D(-70)}) == {"long", "short"}
    assert losing_directions({}) == {"long", "short"}  # cannot tell: lock both
    locks = {"bluechips": new_lock(NOW, {"short"}, D("-0.0221"))}
    assert day_locked(locks, "bluechips", NOW)  # locked at all
    assert day_locked(locks, "bluechips", NOW, "short")
    assert not day_locked(locks, "bluechips", NOW, "long")  # the other way stays open
    assert not day_locked(locks, "alts", NOW, "short")
    info = lock_info(locks, "bluechips", NOW)
    assert info is not None and info["since"] == NOW and info["pnl_at_lock"] == D("-0.0221")
    assert lock_info(locks, "bluechips", NOW + timedelta(hours=12)) is None  # past midnight
    # the first format, a bare "until" string, still means both directions
    old = {"alts": lock_until_midnight(NOW)}
    assert day_locked(old, "alts", NOW, "long") and day_locked(old, "alts", NOW, "short")
    assert lock_info(old, "alts", NOW)["since"] is None  # type: ignore[index]


def test_a_locked_sleeve_does_not_fire_again_on_the_same_loss() -> None:
    stop = D("-0.02")
    shorts_lost = {"short": D(-158)}
    assert directions_to_lock(None, D("-0.019"), stop, shorts_lost) is None  # not at the stop
    assert directions_to_lock(None, D("-0.0221"), stop, shorts_lost) == {"short"}
    info = lock_info({"b": new_lock(NOW, {"short"}, D("-0.0221"))}, "b", NOW)
    # the realized loss that set the lock is still there: it must not close the new longs
    assert directions_to_lock(info, D("-0.0221"), stop, shorts_lost) is None
    assert directions_to_lock(info, D("-0.035"), stop, shorts_lost) is None
    # another full stop's worth of loss since the lock: everything locks
    assert directions_to_lock(info, D("-0.0421"), stop, shorts_lost) == {"long", "short"}
    both = lock_info({"b": new_lock(NOW, {"long", "short"}, D("-0.05"))}, "b", NOW)
    assert directions_to_lock(both, D("-0.20"), stop, shorts_lost) is None  # nothing left to lock
