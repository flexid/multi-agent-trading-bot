from app.triggers import asset_of


def test_asset_is_read_from_the_trigger_reason() -> None:
    assert asset_of("price: SOL moved +4.3% in 1h (> 2×ATR)") == "SOL"
    assert asset_of("polymarket: ETH market 5207000 shifted -16 pp in 1h") == "ETH"
    assert asset_of("x_shock: credible news shock on market") is None
    assert asset_of("x_shock: credible news shock on SPX6900") == "SPX6900"
