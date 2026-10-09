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
