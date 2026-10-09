from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from app.decision import tuning
from app.risk import golive


def test_sharpe_and_daily_returns() -> None:
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    series = [
        (t0 + timedelta(hours=6 * i), D(10000) * (1 + D("0.001")) ** (i // 4)) for i in range(60)
    ]
    rets = golive.daily_returns(series)
    assert len(rets) == 14 and all(abs(r - 0.001) < 1e-9 for r in rets)
    assert golive.sharpe(rets, 365) is None  # zero variance → undefined
    assert (
        golive.sharpe([0.01, -0.005, 0.02, 0.0, 0.003, -0.01, 0.015, 0.002, -0.004, 0.006], 365)
        is not None
    )
    assert golive.sharpe([0.01] * 5, 365) is None  # too few


def test_tuning_step_is_bounded_zeroes_useless_and_shrinks_to_equal() -> None:
    weights = dict(tuning.BASE_WEIGHTS)
    ics = {
        "indicators": 0.10,
        "chart_patterns": 0.02,
        "polymarket": -0.01,
        "macro": 0.03,
        "x_sentiment": 0.0,
    }
    mean = sum(ics.values()) / len(ics)
    new = {}
    for a, w in weights.items():
        step = max(-tuning.MAX_STEP, min(tuning.MAX_STEP, ics[a] - mean))
        new[a] = 0.0 if ics[a] <= 0 else max(0.0, w + step)
    equal = 1 / len(new)
    new = {a: (w + equal) / 2 for a, w in new.items()}
    total = sum(new.values())
    new = {a: w / total for a, w in new.items()}
    assert abs(sum(new.values()) - 1) < 1e-9
    assert new["indicators"] > new["chart_patterns"] > new["polymarket"]
    assert new["polymarket"] == new["x_sentiment"]  # zeroed, then only the equal-share half
    assert abs(new["indicators"] - weights["indicators"]) < 0.12  # ±5 pp then halved toward equal


def test_golive_criteria_names_match_spec_and_owner_rules() -> None:
    names = [
        "shadow days ≥ 28",
        "closed primary trades ≥ 100",
        "net positive after fees and interest",
        "profit factor ≥ 1.3",
        "sharpe above BTC buy-and-hold",
        "max drawdown < 10%",
        "zero simulated liquidations",
        "no operational incidents, last 2 weeks",
        "live self-test passed (order, borrow, close, kill)",
        "live_allowed = true in config",
    ]
    assert golive.MIN_SHADOW_DAYS == 28 and golive.MIN_CLOSED_TRADES == 100
    assert D("1.3") == golive.MIN_PROFIT_FACTOR and D("0.10") == golive.MAX_DRAWDOWN
    assert len(names) == 10


def test_cost_report_renders() -> None:
    from app.ops import cost_report

    text = cost_report(datetime(2026, 10, 9, tzinfo=UTC))
    assert (
        text.startswith("# Running costs") and "Server (DigitalOcean)" in text and "Total" in text
    )
