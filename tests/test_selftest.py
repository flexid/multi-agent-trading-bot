from decimal import Decimal

import pytest

from app import selftest
from app.config import load_config
from app.execution.bybit_models import OrderBook, Side
from app.execution.paper import paper_fill, round_trip_pnl

BOOK = OrderBook.model_validate(
    {"s": "BTCUSDC", "b": [["100.0", "1"]], "a": [["100.2", "1"]], "ts": 1}
)


def test_config_loads_and_maps_spx6900() -> None:
    cfg = load_config()
    assert cfg.symbol("BTC") == "BTCUSDT"
    assert cfg.symbol("SPX6900") == "SPXUSDT"
    assert cfg.trading.live_allowed is False


def test_paper_round_trip_costs_spread_plus_fees() -> None:
    fee = Decimal("0.001")
    entry = paper_fill(BOOK, Side.BUY, Decimal("2"), fee)
    exit_ = paper_fill(BOOK, Side.SELL, Decimal("2"), fee)
    assert entry.price == Decimal("100.2")
    assert exit_.price == Decimal("100.0")
    assert round_trip_pnl(entry, exit_) == Decimal("-0.4") - Decimal("0.2004") - Decimal("0.2")


def test_paper_round_trip_rejects_same_side() -> None:
    fill = paper_fill(BOOK, Side.BUY, Decimal("1"), Decimal(0))
    with pytest.raises(ValueError):
        round_trip_pnl(fill, fill)


@pytest.mark.parametrize("argv", [["--live"], ["--live", "--confirm", "no"]])
def test_live_without_confirmation_exits_before_doing_anything(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise AssertionError("self-test must not start without confirmation")

    monkeypatch.setattr(selftest, "run", boom)
    monkeypatch.setattr(selftest, "get_secrets", boom)
    assert selftest.main(argv) == 2
