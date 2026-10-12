from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.site import snapshot as snap


def minimal() -> snap.Snapshot:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    return snap.Snapshot(
        generated_at=now,
        mode="shadow",
        performance=snap.Performance(
            since=now,
            bot_pct=1.2,
            btc_hold_pct=-0.5,
            basket_pct=0.1,
            drawdown_pct=-2.0,
            equity_curve=[[0, 0.0]],
        ),
        stats=snap.Stats(trades=3, win_rate_pct=66.7, profit_factor=1.4, avg_holding="9 hours"),
        open_trades=[
            snap.OpenTrade(
                asset="SOL",
                cashtag="$SOL",
                direction="long",
                entry=74.1,
                leverage=3,
                stop=72.3,
                target=78,
                time_in_trade="2 hours",
                unrealized_price_pct=0.4,
                unrealized_margin_pct=1.2,
                paper=True,
            )
        ],
        closed_trades=[
            snap.ClosedTrade(
                asset="SOL",
                cashtag="$SOL",
                direction="long",
                entry=74.1,
                exit=77.6,
                leverage=3,
                price_pct=4.7,
                margin_pct=13.6,
                holding="9 hours",
                opened_at=now,
                closed_at=now,
                x_url="https://x.com/decentradork/status/1",
                paper=True,
            )
        ],
        assets=[
            snap.AssetView(
                asset="SOL",
                cashtag="$SOL",
                agents=[
                    snap.AgentScore(
                        agent="indicators",
                        score=0.3,
                        confidence=0.7,
                        valid=True,
                        reasons=["momentum fading on the 4h"],
                    )
                ],
                pm_claude="long",
                pm_gpt="long",
                consensus="long",
                consensus_score=0.4,
                formula_score=0.3,
                reason="PMs agree",
                macro_regime="neutral",
                coupling=0.27,
                macro_tradfi=0.0,
                macro_native=-0.01,
            )
        ],
        leaderboard=[],
        heartbeat_ok=True,
        handle="decentradork",
    )


def test_clean_snapshot_passes() -> None:
    assert snap.verify(minimal()) == []


def test_schema_refuses_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        snap.OpenTrade(**{**minimal().open_trades[0].__dict__, "notional": 5000})


def test_verify_catches_forbidden_keys_and_amounts() -> None:
    s = minimal()
    bad = s.model_copy(
        update={"assets": [s.assets[0].model_copy(update={"reason": "sized 2000 usdt on this"})]}
    )
    problems = snap.verify(bad)
    assert problems and "usdt" in problems[0].lower() or "number" in problems[0]
    data = s.model_dump()
    data["stats"] = {**data["stats"], "cash": 1}
    assert any("forbidden key" in p for p in snap.verify_dict(data))


def test_footer_and_links_are_allowed_on_the_site() -> None:
    s = minimal().model_copy(
        update={
            "assets": [
                minimal()
                .assets[0]
                .model_copy(update={"reason": "nfa. just a bot trading its own bag."})
            ]
        }
    )
    assert snap.verify(s) == []


def test_only_the_soft_stop_is_public() -> None:
    """The hard stop and the exchange backup level never reach the site, the posts or
    the card (owner 2026-10-10): the verifier rejects the keys, the models lack them."""
    from app.site.snapshot import FORBIDDEN_KEYS, OpenTrade, verify_dict

    assert {"hard_stop", "backup_stop_price", "backup_stop_link"} <= FORBIDDEN_KEYS
    assert "hard_stop" not in OpenTrade.model_fields
    assert verify_dict({"open_trades": [{"stop": 1.0, "hard_stop": 0.9}]}) == [
        "forbidden key $.open_trades[0].hard_stop"
    ]
    from app.social.templates import TradeFacts

    assert "hard_stop" not in TradeFacts.__dataclass_fields__


class _FakeSession:
    """Stands in for a session: ``get`` returns the mark, ``execute`` the last candle close."""

    def __init__(self, mark: object | None, close: float | None) -> None:
        self.mark, self.close = mark, close

    def get(self, _model: object, _key: str) -> object | None:
        return self.mark

    def execute(self, _stmt: object) -> "_FakeSession":
        return self

    def scalar_one_or_none(self) -> float | None:
        return self.close


def test_price_now_prefers_a_fresh_quote_over_the_candle() -> None:
    from datetime import timedelta
    from decimal import Decimal

    from app.db.models import Mark

    now = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)
    fresh = Mark(symbol="BTCUSDT", bid=Decimal("100"), ask=Decimal("102"), ts=now)
    assert snap.spot_for(_FakeSession(fresh, 90.0), "BTCUSDT", now) == 101.0
    # A quote older than two minutes is not "now": the executor is down, use the candle.
    stale = Mark(
        symbol="BTCUSDT",
        bid=Decimal("100"),
        ask=Decimal("102"),
        ts=now - timedelta(seconds=snap.MARK_MAX_AGE_S + 1),
    )
    assert snap.spot_for(_FakeSession(stale, 90.0), "BTCUSDT", now) == 90.0
    assert snap.spot_for(_FakeSession(None, None), "BTCUSDT", now) is None


def test_live_feed_passes_the_whitelist() -> None:
    trade = minimal().open_trades[0].model_copy(update={"price": 74.4})
    live = snap.LiveView(
        generated_at=datetime(2026, 10, 12, tzinfo=UTC), mode="shadow", open_trades=[trade]
    )
    assert snap.verify_dict(live.model_dump(mode="json")) == []
    with pytest.raises(ValidationError):
        snap.LiveView.model_validate({**live.model_dump(), "margin": 5})


def test_live_feed_is_not_pushed_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from pathlib import Path

    from app.site import publish

    pushes: list[int] = []
    views = iter(
        snap.LiveView(
            generated_at=datetime(2026, 10, 12, 0, 0, i, tzinfo=UTC), mode="shadow", open_trades=[]
        )
        for i in range(3)
    )
    monkeypatch.setattr(publish, "build_live", lambda snapshot_at=None: next(views))
    monkeypatch.setattr(publish, "snapshot_due", lambda: False)
    monkeypatch.setattr(publish, "OUT", Path(str(tmp_path)) / "snapshot.json")
    monkeypatch.setattr(publish, "_last_live", ("", 0.0))

    def fake_push(objects: dict[str, tuple[bytes, str]], max_age: int = 60) -> bool:
        assert list(objects) == ["live.json"] and max_age == publish.LIVE_MAX_AGE_S
        pushes.append(1)
        return True

    monkeypatch.setattr(publish, "push_r2", fake_push)
    assert publish.publish_live() is True
    assert publish.publish_live() is False  # same content, only the timestamp moved
    assert len(pushes) == 1


def test_a_trade_event_rebuilds_the_snapshot_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner 2026-10-12: a close must show on the site right away, not at the next
    five-minute mark. The live job rebuilds the snapshot when a position opened or closed
    after the last one, and tells the page its time."""
    from app.site import publish

    t0 = datetime(2026, 10, 12, 17, 0, tzinfo=UTC)
    monkeypatch.setattr(publish, "_last_snapshot_at", None)
    monkeypatch.setattr(publish, "last_trade_event", lambda now=None: t0 + timedelta(minutes=1))
    assert publish.snapshot_due() is False  # nothing published yet by this process
    monkeypatch.setattr(publish, "_last_snapshot_at", t0)
    assert publish.snapshot_due() is True  # a close one minute after the snapshot
    monkeypatch.setattr(publish, "last_trade_event", lambda now=None: t0 - timedelta(minutes=1))
    assert publish.snapshot_due() is False
    monkeypatch.setattr(publish, "last_trade_event", lambda now=None: None)
    assert publish.snapshot_due() is False
    # the live feed carries the snapshot's time, and the whitelist allows it
    trade = minimal().open_trades[0].model_copy(update={"price": 74.4})
    live = snap.LiveView(generated_at=t0, mode="shadow", open_trades=[trade], snapshot_at=t0)
    assert snap.verify_dict(live.model_dump(mode="json")) == []
