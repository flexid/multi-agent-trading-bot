from datetime import UTC, datetime

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
