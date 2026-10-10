"""Optima, the read-only cost and execution adviser (crew member 15)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from app.agents import optima
from app.agents.names import CREW
from app.config import get_config
from app.memo import improvement_prompt, optima_section, render

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def test_optima_is_the_fifteenth_crew_member_and_advisory_only() -> None:
    ids = [m.id for m in CREW]
    assert "optima" in ids and len(CREW) == 15
    m = next(m for m in CREW if m.id == "optima")
    assert m.name == "Optima" and "advis" in m.role
    assert not any(ch.isdigit() for ch in m.blurb)  # the site whitelist
    assert get_config().models.optima == "claude-opus-5-5"
    assert not hasattr(optima, "apply")  # nothing in the module changes anything


def test_pure_helpers() -> None:
    assert optima.ic_per_dollar(0.05, 10.0) == 0.005
    assert optima.ic_per_dollar(0.05, 0.0) == 5.0  # a free reading ranks first, finitely
    assert optima.ic_per_dollar(None, 10.0) is None
    assert optima.slippage_bps(D("101"), D("100"), "long") == 100.0  # paid more
    assert optima.slippage_bps(D("101"), D("100"), "short") == -100.0  # got more: better
    f = optima.forecast_month(100.0, 10.0, NOW)
    assert f == {
        "month_to_date_usd": 100.0,
        "daily_rate_usd": 10.0,
        "days_left": 21,
        "forecast_usd": 310.0,
    }
    assert optima._p90([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]) == 10.0
    assert optima._p90([]) is None and optima._mean([]) is None
    prop = {"entry": "99", "plan": {"entry": "100"}, "plan_max": {"entry": "101"}}
    assert optima.planned_entry(prop, "primary") == D(100)
    assert optima.planned_entry(prop, "max") == D(101)
    assert optima.planned_entry({"entry": "99"}, "primary") == D(99)
    assert optima.planned_entry(None, "primary") is None


def test_report_schema_is_strict() -> None:
    report = optima.OptimaReport.model_validate(
        {
            "summary": "Spend is on track.",
            "findings": [
                {
                    "area": "cost_value",
                    "severity": "low",
                    "title": "Orak costs little and has no IC yet",
                    "detail": "Polymarket mapping is cheap; the IC sample is too small.",
                    "evidence": ["polymarket usd_month 0.40", "sample 12"],
                }
            ],
            "proposals": [
                {
                    "title": "Keep Jev for the alts",
                    "change": "No change.",
                    "expected_effect": "Same labels at a fraction of the cost.",
                    "cost_delta_usd_month": -3.5,
                    "risk": "None.",
                    "needs": "none",
                }
            ],
            "budget_on_track": True,
        }
    )
    assert report.findings[0].area is optima.Area.COST_VALUE
    with pytest.raises(ValidationError):
        optima.OptimaReport.model_validate({"summary": "x", "findings": [], "proposals": []})
    with pytest.raises(ValidationError):  # unknown area, extra field
        optima.OptimaReport.model_validate(
            {
                "summary": "x",
                "findings": [
                    {
                        "area": "vibes",
                        "severity": "low",
                        "title": "t",
                        "detail": "d",
                        "evidence": [],
                    }
                ],
                "proposals": [],
                "budget_on_track": True,
                "apply": True,
            }
        )


def _optima_facts() -> dict[str, object]:
    return {
        "as_of": "2026-10-10 05:30 UTC",
        "model": "claude-opus-5-5",
        "error": None,
        "budget": {
            "month_to_date_usd": 80.0,
            "daily_rate_usd": 9.5,
            "forecast_usd": 279.5,
            "budget_usd": 250.0,
        },
        "report": {
            "summary": "Forecast runs over budget by 12%.",
            "findings": [
                {
                    "area": "budget",
                    "severity": "high",
                    "title": "Over budget at this rate",
                    "detail": "The PM seats cost most.",
                    "evidence": ["forecast 279.5 of 250"],
                }
            ],
            "proposals": [
                {
                    "title": "Drop the alt PM twin to Sonnet",
                    "change": "models.shadow_alt.pm_2 = claude-sonnet-5-5",
                    "expected_effect": "Saves about $30 a month.",
                    "cost_delta_usd_month": -30.0,
                    "risk": "A weaker comparison seat.",
                    "needs": "owner",
                }
            ],
            "budget_on_track": False,
        },
    }


def test_memo_renders_optima_as_its_own_section_and_the_brief_lists_its_proposals() -> None:
    lines = optima_section(_optima_facts())
    text = "\n".join(lines)
    assert "## Optima (cost, execution, budget), as of 2026-10-10 05:30 UTC" in text
    assert "forecast $279.50 of $250" in text
    assert "[high] budget: **Over budget at this rate**" in text
    assert "1. **Drop the alt PM twin to Sonnet** (needs: owner, -30.00 $/month)" in text
    assert "\n".join(optima_section(None)).endswith("(no Optima run this week)")
    facts = {
        "week_ending": "2026-10-13",
        "mode": "shadow",
        "pilot_enabled": True,
        "cycles": {"scheduled": 42, "triggered": 3, "failed": 0, "cost_usd": 30.0},
        "llm_budget_usd": 250.0,
        "closed_trades_by_track": {},
        "ledgers_equity": {},
        "pilot_vs_paper": [],
        "leaderboard": [],
        "agent_weights": {},
        "risk_rule_hits": [],
        "posts": {},
        "failed_fetches": {},
        "polymarket_coverage": {},
        "x_mentions": {},
        "optima": _optima_facts(),
    }
    memo_text = render(facts, None, "no proposals")
    assert memo_text.index("## Optima") < memo_text.index("## Proposals for review")
    brief = improvement_prompt(facts, None)
    assert "## Optima's proposals (cost, execution, budget)" in brief
    assert "- [ ] O1. Drop the alt PM twin to Sonnet (needs: owner)" in brief
