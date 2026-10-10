"""Logic v5: the catalyst agent (Djaf, TypeSafe Jev) for the alt sleeve."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest

from app.agents import catalysts as cat
from app.agents import x_sentiment as xs
from app.agents.jev_questions import event_questions, post_questions
from app.config import get_config
from app.db.models import CatalystEvent, XPostRecord
from app.llm import LLMError, Prompt

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
ASSETS = ["PEPE", "HBAR", "PUMP", "SPX6900", "ENA"]


def event(
    i: int,
    asset: str | None = "ENA",
    kind: str = "listing",
    direction: str = "bullish",
    materiality: float = 0.8,
    age_h: float = 2,
    source: str = "bybit",
    shadow: dict[str, object] | None = None,
) -> CatalystEvent:
    return CatalystEvent(
        id=i,
        source=source,
        source_id=str(i),
        text="x",
        event_at=NOW - timedelta(hours=age_h),
        fetched_at=NOW,
        asset=asset,
        type=kind,
        direction=direction,
        materiality=D(str(materiality)),
        classified_at=NOW,
        shadow=shadow,
    )


def test_config_wires_djaf_into_the_alt_sleeve_only() -> None:
    cfg = get_config()
    assert cat.sleeve_assets(cfg) == ASSETS
    assert cfg.sleeves["bluechips"].weights.get("catalysts", 0.0) == 0.0
    assert cfg.sleeves["alts"].models["x_sentiment"].startswith("jev-")
    assert cfg.models.catalysts.startswith("jev-")
    assert set(cfg.catalysts.unlock_slugs) == {"ENA", "PUMP", "HBAR"}
    assert "spx6900" in cfg.catalysts.accounts
    # the admin container never sees the key: the env whitelist is explicit
    from pathlib import Path

    assert "JEV" not in Path("scripts/admin_env.sh").read_text()


def test_pre_filter_names_the_asset_and_ignores_the_rest() -> None:
    assert cat.mentioned_asset("Bybit to delist ENAUSDT margin pair", ASSETS) == "ENA"
    assert cat.mentioned_asset("New listing: $SPX perpetual", ASSETS) == "SPX6900"
    assert cat.mentioned_asset("pump.fun launches livestreams", ASSETS) == "PUMP"
    assert cat.mentioned_asset("Hedera council adds a member", ASSETS) == "HBAR"
    assert cat.mentioned_asset("BTC ETF inflows hit a record", ASSETS) is None
    assert cat.mentioned_asset("a pump in volume today", ASSETS) is None  # plain word, no token


def test_jev_questions_cover_every_label_with_a_no_match_option() -> None:
    q = event_questions({a: cat.ASSET_TEXT[a] for a in ASSETS})
    assert set(q) == {"asset", "type", "direction", "material"}
    assert "none" in q["asset"].criteria and set(q["type"].criteria) == set(cat.TYPE_WEIGHT)
    pq = post_questions(2, {"BTC": "Bitcoin"})
    assert {k.split("_", 1)[1] for k in pq} == {"asset", "stance", "kind", "cred", "shock"}
    assert len(pq) == 10


def test_text_events_decay_by_type_and_leave_the_window() -> None:
    fresh = cat.event_weight(event(1, age_h=0), NOW)
    assert fresh == pytest.approx(0.8)  # sign × materiality × type weight, no decay yet
    half = cat.event_weight(event(2, age_h=cat.HALF_LIFE_H["listing"]), NOW)
    assert half == pytest.approx(0.4, abs=0.01)
    assert cat.event_weight(event(3, age_h=8 * 24), NOW) == 0.0  # outside the 7-day window
    assert cat.event_weight(event(4, direction="bearish", kind="hack"), NOW) < 0
    assert cat.event_weight(event(5, materiality=0.1), NOW) == 0.0  # below the floor
    assert cat.event_weight(event(6, asset=None), NOW) == 0.0  # "none" never counts
    assert cat.event_weight(event(7, direction="neutral"), NOW) == 0.0


def test_unlock_pressure_ramps_before_the_date_and_fades_after() -> None:
    assert cat.unlock_weight(NOW + timedelta(days=10), NOW) == 0.0
    assert cat.unlock_weight(NOW + timedelta(days=3.5), NOW) == pytest.approx(0.5)
    assert cat.unlock_weight(NOW, NOW) == 1.0
    assert cat.unlock_weight(NOW - timedelta(days=1.5), NOW) == pytest.approx(0.5)
    assert cat.unlock_weight(NOW - timedelta(days=4), NOW) == 0.0
    assert cat.unlock_materiality(2.0) == 1.0 and cat.unlock_materiality(0.5) == 0.25


def test_unlocks_from_a_defillama_schedule_keep_cliffs_and_drop_the_daily_drip() -> None:
    day = 86400
    t0 = int(NOW.timestamp())
    data = {
        "supplyMetrics": {"adjustedSupply": 1_000_000, "maxSupply": 2_000_000},
        "metadata": {
            "unlockEvents": [
                {  # a 5% cliff in 10 days
                    "timestamp": t0 + 10 * day,
                    "cliffAllocations": [
                        {"recipient": "Team", "amount": 30_000},
                        {"recipient": "Investors", "amount": 20_000},
                    ],
                },
                {"timestamp": t0 + 60 * day, "cliffAllocations": [{"amount": 50_000}]},  # too far
                {"timestamp": t0 - 10 * day, "cliffAllocations": [{"amount": 50_000}]},  # past
            ]
        },
        "documentedData": {
            "data": [
                {  # a linear drip of 0.01%/day: not an event; one 0.5% step: an event
                    "label": "Ecosystem",
                    "data": [
                        {"timestamp": t0 + i * day, "unlocked": 100 * i + (5000 if i >= 5 else 0)}
                        for i in range(0, 8)
                    ],
                }
            ]
        },
    }
    unlocks = cat.unlocks_from_schedule("ENA", data, NOW, 30)
    assert [(u.at.date().isoformat(), round(u.supply_pct, 2)) for u in unlocks] == [
        ("2026-10-15", 0.51),
        ("2026-10-20", 5.0),
    ]
    assert unlocks[1].recipients == "Investors, Team"
    assert cat.unlocks_from_schedule("ENA", {"supplyMetrics": {}}, NOW, 30) == []


def test_score_sums_events_with_evidence_from_labels_only() -> None:
    rows = [
        event(1, kind="listing", direction="bullish", materiality=0.9),
        event(2, kind="partnership", direction="bullish", materiality=0.5, age_h=30, source="x"),
        event(3, asset="PUMP", kind="hack", direction="bearish", materiality=0.9),
    ]
    rows[1].author = "ethena"
    score, conf, evidence, flags = cat.score_events(rows, "ENA", NOW)
    assert 0.9 < score <= 1.0 and conf > 0.7
    assert any("listing bullish via bybit" in e for e in evidence)
    assert any("partnership bullish via @ethena" in e for e in evidence)
    assert all(e != "x" for e in evidence)  # never the text
    assert flags == []
    s2, _, _, flags2 = cat.score_events(rows, "PUMP", NOW)
    assert s2 < -0.8 and any("hack" in f for f in flags2)
    s3, c3, ev3, _ = cat.score_events(rows, "HBAR", NOW)
    assert s3 == 0.0 and c3 == 0.0 and ev3 == ["no material catalyst in the window"]


def test_unlock_event_scores_as_bearish_pressure_with_a_flag() -> None:
    row = event(9, kind="unlock", direction="bearish", materiality=0.6, source="unlocks")
    row.event_at = NOW + timedelta(days=1)
    row.supply_pct = D("1.2")
    score, _, evidence, flags = cat.score_events([row], "ENA", NOW)
    assert score == pytest.approx(-0.6 * 0.8 * (1 - 1 / 7), abs=0.01)
    assert evidence[0].startswith("unlock in 1d: 1.20% of supply")
    assert flags == [f"unlock pressure ({score:+.2f})"]


def test_shadow_labels_score_separately_and_agreement_is_counted() -> None:
    agree = {"asset": "ENA", "type": "listing", "direction": "bullish", "materiality": 0.8}
    differ = {"asset": "ENA", "type": "other", "direction": "neutral", "materiality": 0.1}
    rows = [event(1, shadow=agree), event(2, shadow=differ), event(3, shadow=None)]
    main, _, _, _ = cat.score_events(rows, "ENA", NOW)
    shadow, _, _, _ = cat.score_events(rows, "ENA", NOW, shadow=True)
    assert main > shadow > 0
    a = cat.agreement(rows)
    assert (a.events, a.asset, a.type, a.direction, a.all_three) == (2, 1.0, 0.5, 0.5, 0.5)
    assert cat.agreement([]).events == 0


def test_alt_sleeve_posts_go_to_jev_and_the_rest_to_the_text_model() -> None:
    cfg = get_config()
    alt = XPostRecord(id="1", text="x", fetched_at=NOW, query_asset="PEPE")
    blue = XPostRecord(id="2", text="x", fetched_at=NOW, query_asset="BTC")
    curated = XPostRecord(id="3", text="x", fetched_at=NOW, author="saylor")
    project = XPostRecord(id="4", text="x", fetched_at=NOW, author="ethena")
    assert xs.labeler_for(cfg, alt).startswith("jev-")
    assert xs.labeler_for(cfg, blue) == cfg.models.x_sentiment
    assert xs.labeler_for(cfg, curated) == cfg.models.x_sentiment
    assert xs.labeler_for(cfg, project).startswith("jev-")
    assert xs.labeler_for_asset(cfg, "ENA").startswith("jev-")


def test_jev_post_answers_become_a_post_label() -> None:
    answers = {
        "p0_asset": {"type": "choice", "choice": "PEPE", "probabilities": {}, "confidence": 0.9},
        "p0_stance": {
            "type": "choice",
            "choice": "bullish",
            "probabilities": {},
            "confidence": 0.9,
        },
        "p0_kind": {"type": "choice", "choice": "news", "probabilities": {}, "confidence": 0.9},
        "p0_cred": {
            "type": "score",
            "score": 2.4,
            "probabilities": {},
            "confidence": 0.8,
            "legend": {},
        },
        "p0_shock": {"type": "noul", "noul": 0.7},
    }
    label = xs._post_label_from_answers("42", answers, 0)
    assert (label.id, label.asset, label.stance.value, label.kind.value) == (
        "42",
        "PEPE",
        "bullish",
        "news",
    )
    assert label.credibility == pytest.approx(0.8) and label.shock is True
    with pytest.raises(KeyError):
        xs._post_label_from_answers("42", answers, 1)


class FakeJev:
    name = "typesafe"

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def judge(self, **kwargs: object) -> object:
        from app.llm.base import Usage
        from app.llm.jev_client import Judgment

        self.calls += 1
        if self.calls <= self.fail_times:
            raise LLMError("boom")
        return Judgment(
            model="jev-1.13.0",
            answers={"q": {"type": "noul", "noul": 0.9}},
            usage=Usage(input_tokens=100, output_tokens=0),
        )


async def test_judge_retries_once_logs_cost_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import llm

    logged: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(llm, "_log_judgment", lambda *a, **k: logged.append(("ok", a)))
    monkeypatch.setattr(llm, "_log_call", lambda *a, **k: logged.append(("fail", a)))
    once = FakeJev(fail_times=1)
    monkeypatch.setattr(llm, "_jev", lambda: once)
    result = await llm.judge(
        "catalysts", version=1, state={"a": 1}, questions={"q": {}}, model="jev-latest"
    )
    assert result.answers["q"]["noul"] == 0.9 and once.calls == 2
    assert logged[-1][0] == "ok" and logged[-1][1][7] == 2  # attempts
    twice = FakeJev(fail_times=2)
    monkeypatch.setattr(llm, "_jev", lambda: twice)
    with pytest.raises(LLMError):
        await llm.judge("catalysts", version=1, state={}, questions={"q": {}}, model="jev-latest")
    assert twice.calls == 2 and logged[-1][0] == "fail"
    with pytest.raises(LLMError):  # a text model is not a judge
        await llm.judge("catalysts", version=1, state={}, questions={}, model="claude-sonnet-5-5")
    with pytest.raises(LLMError):  # and a jev model is not a text model
        llm.provider_for("jev-latest")
    # cost: $0.042 per million input tokens, output free
    from app.llm.base import Usage, cost_usd

    assert cost_usd(get_config().llm.pricing, "jev-1.13.0", Usage(1_000_000, 500)) == D("0.042")
    assert Prompt("catalysts", 1, "").version == 1
