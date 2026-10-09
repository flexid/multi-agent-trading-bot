from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.agents import macro
from app.agents.macro import MacroInputs, MacroView, Regime


def test_z_changes_flags_a_spike() -> None:
    idx = pd.date_range("2026-07-01", periods=90, freq="D", tz="UTC")
    rng = np.random.default_rng(0)
    calm = 100 + rng.standard_normal(90).cumsum() * 0.1
    spiked = calm.copy()
    spiked[-5:] *= 1.10
    wide = pd.DataFrame({"SP500": spiked, "VIXCLS": calm}, index=idx)
    z = macro.z_changes(wide)
    assert z["SP500"] > 3
    assert abs(z["VIXCLS"]) < 3


def test_calendar_events_and_today(tmp_path: Path) -> None:
    cal = tmp_path / "cal.toml"
    cal.write_text('[[events]]\nkind = "FOMC"\ndates = ["2026-10-28"]\n')
    assert macro.events_within(48, datetime(2026, 10, 26, 12, tzinfo=UTC), cal) == [
        "FOMC 2026-10-28"
    ]
    assert macro.events_within(48, datetime(2026, 10, 20, tzinfo=UTC), cal) == []
    assert macro.event_today(datetime(2026, 10, 28, 15, tzinfo=UTC), cal)
    assert not macro.event_today(datetime(2026, 10, 29, 1, tzinfo=UTC), cal)


def test_shipped_calendar_covers_the_year() -> None:
    assert len(macro.events_within(24 * 400, datetime(2026, 1, 1, tzinfo=UTC))) >= 30


def test_coupling_is_positive_part_only() -> None:
    idx = pd.date_range("2026-07-01", periods=60, freq="D", tz="UTC")
    rng = np.random.default_rng(1)
    base = rng.standard_normal(60)
    sp = pd.Series(100 * np.exp(np.cumsum(base * 0.01)), index=idx)
    asset_follows = pd.Series(50 * np.exp(np.cumsum(base * 0.02)), index=idx)
    asset_inverse = pd.Series(50 * np.exp(np.cumsum(-base * 0.02)), index=idx)
    wide = pd.DataFrame({"SP500": sp})
    assert macro.coupling(asset_follows, wide) > 0.9
    assert macro.coupling(asset_inverse, wide) == 0.0  # decoupled, never inverted


def test_output_scales_regime_by_confidence_and_coupling() -> None:
    inputs = MacroInputs(
        z_changes={"VIXCLS": 2.0},
        events_48h=["CPI 2026-10-14"],
        fed_odds={},
        as_of=datetime.now(UTC),
    )
    view = MacroView(
        regime=Regime.RISK_OFF, regime_confidence=0.8, event_risk_48h=0.7, reasons=["vix up"]
    )
    coupled = macro.to_output("BTC", view, 1.0, inputs, 3)
    decoupled = macro.to_output("SPX6900", view, 0.0, inputs, 3)
    assert coupled.score == -0.8 and decoupled.score == 0.0
    assert coupled.confidence > decoupled.confidence
    assert any("CPI" in f for f in coupled.risk_flags) and any(
        "event risk" in f for f in coupled.risk_flags
    )
