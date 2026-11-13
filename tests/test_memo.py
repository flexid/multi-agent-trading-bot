from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.models import Position
from app.memo import Memo, Proposal, pilot_vs_paper, render

NOW = datetime(2026, 10, 9, 18, tzinfo=UTC)


def pos(
    track: str, decision: int, entry: str, exit_: str | None, direction: str = "short"
) -> Position:
    return Position(
        track=track,
        mode="pilot" if track == "pilot" else "paper",
        asset="ETH",
        symbol="ETHUSDT",
        direction=direction,
        decision_id=decision,
        qty=Decimal("0.01"),
        entry_price=Decimal(entry),
        exit_price=Decimal(exit_) if exit_ else None,
        leverage=Decimal(1),
        notional=Decimal(25),
        margin=Decimal(25),
        stop=Decimal("2554"),
        target=Decimal("2344"),
        max_hold_hours=48,
        status="closed" if exit_ else "open",
        opened_at=NOW - timedelta(hours=5),
        closed_at=NOW if exit_ else None,
        fees=Decimal("0.03"),
        interest=Decimal("0.001"),
    )


def test_pilot_vs_paper_measures_slippage_against_the_paper_twin() -> None:
    pilot = [pos("pilot", 12, "2488.91", "2470.00"), pos("pilot", 99, "100", None)]
    paper = [pos("primary", 12, "2489.28", "2469.00"), pos("primary", 50, "1", "1")]
    rows = pilot_vs_paper(pilot, paper)
    assert len(rows) == 1 and rows[0]["asset"] == "ETH" and rows[0]["closed"]
    # a short that sold lower than the simulator paid worse on entry; covered higher = worse exit
    assert rows[0]["entry_worse_bps"] > 0 and rows[0]["exit_worse_bps"] > 0
    long_rows = pilot_vs_paper(
        [pos("pilot", 1, "101", "110", "long")], [pos("primary", 1, "100", "111", "long")]
    )
    assert long_rows[0]["entry_worse_bps"] > 0 and long_rows[0]["exit_worse_bps"] > 0


def test_render_includes_facts_and_proposals_and_degrades_without_a_model() -> None:
    f = {
        "week_ending": "2026-10-09",
        "mode": "shadow",
        "pilot_enabled": True,
        "cycles": {"scheduled": 6, "triggered": 1, "failed": 0, "cost_usd": 4.2},
        "llm_budget_usd": 250.0,
        "ledgers_equity": {"primary": 9990.0},
        "closed_trades_by_track": {
            "primary": {
                "closed": 4,
                "wins": 2,
                "pnl": 8.4,
                "profit_factor": 1.53,
                "avg_hold_h": 5.0,
                "reasons": {"kill": 4},
            },
            "max": {"closed": 0},
            "pilot": {"closed": 0},
        },
        "open_positions": [],
        "pilot_vs_paper": [
            {
                "asset": "ETH",
                "direction": "short",
                "entry_worse_bps": 1.5,
                "exit_worse_bps": None,
                "pilot_fees": 0.03,
                "pilot_interest": 0.0,
                "closed": False,
            }
        ],
        "llm_cost_by_task": {},
        "risk_rule_hits": [],
        "posts": {"sent": 5, "blocked_by_whitelist": 0, "template_fallbacks": 1},
        "failed_fetches": {},
        "agent_weights": {"indicators": 0.25},
        "leaderboard": [
            {"name": "indicators", "kind": "agent", "ic_1d": None, "ic_4h": None, "sample": 3}
        ],
        "polymarket_coverage": {"BTC": "ok"},
        "x_mentions": {},
    }
    memo = Memo(
        summary="Quiet week.",
        keep=["stops"],
        proposals=[
            Proposal(
                title="Wait", evidence="4 trades", change="none yet", risk="none", effort="small"
            )
        ],
    )
    text = render(f, memo)
    assert "6 scheduled + 1 triggered" in text and "primary: 4 closed, 2 won" in text
    assert "ETH short: entry +1.5 bps worse, still open" in text
    assert "1. **Wait** (small)" in text and "Nothing in this memo changes the bot" in text
    assert "(no proposals: model down)" in render(f, None, "model down")


def test_memo_markdown_becomes_clean_html_for_mail() -> None:
    from app.admin.notify import to_html

    md = (
        "# Title\n\nIntro **bold** line.\n\n## Trades\n- one\n- two\n\n## Proposals\n"
        "1. **Wait** (small)\n   - evidence: 4 trades\n   - change: none\n"
        "2. **Next** (medium)\n   - evidence: x\n\nFooter <tag>.\n"
    )
    out = to_html(md)
    assert "<h1" in out and "Title</h1>" in out and "<b>bold</b>" in out
    assert out.count("<li style='margin:3px 0'>") == 2 and "<ol" in out
    assert out.count("<li style='margin:8px 0'>") == 2 and out.count("<ul style='margin:4px") == 2
    assert "&lt;tag&gt;" in out and "**" not in out
    assert out.count("<ol") == out.count("</ol>") and out.count("<ul") == out.count("</ul>")


def test_improvement_brief_lists_proposals_as_checkboxes() -> None:
    from app.memo import improvement_prompt

    memo = Memo(
        summary="s",
        keep=["stops"],
        proposals=[
            Proposal(
                title="Wait", evidence="4 trades", change="none yet", risk="none", effort="small"
            ),
            Proposal(
                title="Tune X",
                evidence="IC +0.1",
                change="x_sentiment 0.2",
                risk="noise",
                effort="small",
            ),
        ],
    )
    brief = improvement_prompt({"week_ending": "2026-10-09"}, memo)
    assert "- [ ] 1. Wait (small)" in brief and "- [ ] 2. Tune X (small)" in brief
    assert "change: x_sentiment 0.2" in brief and "## Owner notes" in brief and "- stops" in brief
    assert "(the memo produced no proposals" in improvement_prompt({"week_ending": "x"}, None)


def test_the_memo_facts_state_the_logic_in_force() -> None:
    """Owner 2026-10-26: the brief cited a consensus rule and books that were gone. The
    facts now carry the version, the rule, the active books and what is decided."""
    from app.memo import decided_log, logic_facts
    from app.version import FROZEN_UNTIL, LOGIC_VERSION

    f = logic_facts(["primary", "pilot"], ["max", "inverse"])
    assert f["version"] == LOGIC_VERSION and f["frozen_until"] == FROZEN_UNTIL
    assert "two of the three managers" in f["consensus"]
    assert f["closed_books"] == ["max", "inverse"]
    log = decided_log()
    assert log and log[0].startswith(f"v{LOGIC_VERSION} (")


def test_excursion_tracks_best_and_worst() -> None:
    from decimal import Decimal as D

    from app.execution.simulator import Quote, Side, excursion

    now = datetime.now(UTC)
    mfe, mae = excursion(Side.LONG, D(100), Quote(D(101), D("101.1"), now), None, None)
    assert mfe == D("0.01") and mae == D(0)
    mfe, mae = excursion(Side.LONG, D(100), Quote(D(97), D("97.1"), now), mfe, mae)
    assert mfe == D("0.01") and mae == D("-0.03")
    mfe, mae = excursion(Side.SHORT, D(100), Quote(D("98.9"), D(99), now), None, None)
    assert mfe == D("0.01") and mae == D(0)


def test_the_brief_states_the_restated_goal_and_not_the_retired_one() -> None:
    """Owner 2026-11-12: the 1%-a-day goal is retired; the brief and the prompt say what
    replaced it and how a rule change and the managers are judged."""
    from app.llm import load_prompt
    from app.memo import GOAL, improvement_prompt

    brief = improvement_prompt({"week_ending": "2026-11-16"}, None)
    assert GOAL in brief and "retired" in brief and "skill against drift" in brief
    assert "an average of 1% per day" not in brief
    prompt = load_prompt("memo")
    assert prompt.version == 11 and "`promotion`" in prompt.system
    assert "`failing_sources`" in prompt.system
    assert "1% a day is retired" in prompt.system and "skill_vs_drift" in prompt.system
    assert "promotion_rule" in prompt.system and "two sleeves" not in prompt.system


def test_render_shows_the_slow_books_the_skill_table_and_the_cost_share() -> None:
    from app.memo import render

    f = {
        "week_ending": "2026-11-16",
        "goal": "beat cash",
        "mode": "shadow",
        "pilot_enabled": True,
        "cycles": {"scheduled": 6, "triggered": 1, "failed": 0, "cost_usd": 4.2},
        "llm_budget_usd": 250.0,
        "ledgers_equity": {"primary": 9990.0},
        "closed_trades_by_track": {},
        "open_positions": [],
        "pilot_vs_paper": [],
        "leaderboard": [],
        "agent_weights": {},
        "risk_rule_hits": [],
        "posts": {},
        "failed_fetches": {},
        "polymarket_coverage": {},
        "x_mentions": {},
        "books": {
            "main_book": "managers",
            "promotion_rule": "positive skill and a better book",
            "books": {
                "trend": {
                    "track": "trend",
                    "since": "2026-11-12",
                    "equity": 10012.5,
                    "return_pct": 0.13,
                    "open": ["BTC long"],
                    "closed": 2,
                    "wins": 1,
                    "realized": 12.5,
                    "exits": {"signal": 2},
                },
                # a book in cash from its first day: a ledger, no position, no date
                "short": {
                    "track": "short",
                    "since": None,
                    "equity": 10000.0,
                    "return_pct": 0.0,
                    "open": [],
                    "closed": 0,
                    "wins": 0,
                    "realized": 0.0,
                    "exits": {},
                },
                # a book whose ledger the executor has not created yet
                "challenger": {
                    "track": "seats",
                    "since": None,
                    "equity": None,
                    "return_pct": None,
                    "open": [],
                    "closed": 0,
                    "wins": 0,
                    "realized": 0.0,
                    "exits": {},
                },
            },
        },
        "failing_sources": [
            {
                "source": "x.curated",
                "parts": 4,
                "since": "2026-11-06 04:02",
                "errors_in_a_row": 3632,
                "last_error": "/tweets/search/recent: HTTP 402: Payment Required",
            }
        ],
        "promotion": {
            "window_days": 90,
            "calls": 412,
            "days": 31,
            "skill_3d_pct": -0.85,
            "skill_7d_pct": None,
            "skill_half_met": False,
            "missing": ["31 of 60 days", "skill at three days not above zero"],
        },
        "skill_vs_drift": [
            {
                "who": "consensus (2 of 3)",
                "calls": 40,
                "days": 12,
                "short_share": 0.9,
                "1d": {"n": 38, "mean_pct": 0.3, "drift_pct": 0.6, "skill_pct": -0.3, "right": 0.5},
                "3d": {"n": 30, "mean_pct": 1.1, "drift_pct": 1.7, "skill_pct": -0.6, "right": 0.6},
                "7d": {
                    "n": 0,
                    "mean_pct": None,
                    "drift_pct": None,
                    "skill_pct": None,
                    "right": None,
                },
            }
        ],
        "costs": {
            "exchange_fees_usd": 12.0,
            "models_usd": 48.0,
            "main_book_equity": 9990.0,
            "share_of_equity_pct": 0.601,
        },
    }
    text = render(f, None, "no model")
    assert "Goal: beat cash. Main book: managers." in text
    assert "- trend (ledger trend): equity 10012.5, +0.13% since 2026-11-12; holds BTC long" in text
    assert "2 closed, 1 won, realised +12.50" in text and "promotion rule: positive skill" in text
    assert "- short (ledger short): equity 10000.0, +0.00%; holds nothing; 0 closed" in text
    assert "- challenger (ledger seats): no ledger yet; holds nothing" in text
    assert "None" not in text
    assert (
        "- INPUT FAILING NOW: x.curated (4 queries): no success since 2026-11-06 04:02 UTC, "
        "3632 errors in a row; last answer: /tweets/search/recent: HTTP 402: Payment Required"
        in text
    )
    assert (
        "- promotion check, consensus over 90 days: 412 calls on 31 days, skill 3d -0.85, "
        "7d none yet: not met (31 of 60 days; skill at three days not above zero)" in text
    )
    assert "consensus (2 of 3): 40 calls on 12 days; 1d -0.30 on 38; 3d -0.60 on 30" in text
    assert (
        "exchange fees $12.00 + models $48.00 this week = 0.60% of the main book's equity" in text
    )
