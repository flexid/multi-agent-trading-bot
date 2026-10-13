"""The OAuth helper writes each arena trader's key under its own names (2026-10-13)."""

from pathlib import Path

import pytest

from app import bybit_authorize as ba


def test_each_trader_gets_its_own_env_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / ".env"
    env.write_text("BYBIT_API_KEY=old\nOTHER=1\n")
    monkeypatch.setattr(ba, "ENV_PATH", env)
    ba._write_env("k1", "s1", "kwikton")
    ba._write_env("k2", "s2", "kwakton")
    ba._write_env("k0", "s0")  # dorkbot's own, unchanged names
    text = env.read_text()
    assert "COMPETITION_KWIKTON_API_KEY=k1" in text and "COMPETITION_KWIKTON_API_SECRET=s1" in text
    assert "COMPETITION_KWAKTON_API_KEY=k2" in text
    assert "BYBIT_API_KEY=k0" in text and "OTHER=1" in text
    assert text.count("BYBIT_BASE_URL=") == 1  # shared, written once
    with pytest.raises(SystemExit):
        ba.env_names("donald")
