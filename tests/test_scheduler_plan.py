"""Which jobs the scheduler runs before and after the takeover switch (v16)."""

from __future__ import annotations

from typing import Any

from app.config import Config, StrategyConfig, get_config
from app.scheduler import plan
from app.strategy.books import book_tracks, slow_main, track_books


def cfg(**strategy: Any) -> Config:
    return get_config().model_copy(update={"strategy": StrategyConfig(**strategy)})


def test_before_the_switch_the_managers_keep_their_cadence_and_the_rule_cycle_runs_beside() -> None:
    c = cfg(enabled=True)
    assert not slow_main(c)
    assert plan(c) == {
        "cycle_hour": f"*/{c.trading.cycle_hours}",
        "triggers": True,
        "light_cycle": c.triggers.light_cycle,
        "goal_cycle": True,
        "x_poll_starts_cycles": True,
        "rule_cycle": True,
    }
    assert book_tracks(c) == {"trend": "trend", "short": "short", "challenger": "seats"}
    assert track_books(c) == {"trend": "trend", "short": "short", "seats": "challenger"}


def test_after_the_switch_one_cycle_a_day_and_no_triggers() -> None:
    c = cfg(enabled=True, main_book="trend")
    assert slow_main(c)
    assert plan(c) == {
        "cycle_hour": "0",  # 00:02 UTC: the call the challenger trades
        "triggers": False,
        "light_cycle": False,
        "goal_cycle": False,
        "x_poll_starts_cycles": False,  # it still labels posts for the news-shock exit
        "rule_cycle": True,
    }
    assert book_tracks(c)["trend"] == "primary"


def test_without_the_strategy_nothing_changes() -> None:
    c = cfg()
    assert not slow_main(c) and book_tracks(c) == {}
    p = plan(c)
    assert p["rule_cycle"] is False and p["triggers"] is True and p["goal_cycle"] is True
    off_books = cfg(enabled=True, short_track="", challenger_track="")
    assert book_tracks(off_books) == {"trend": "trend"}
