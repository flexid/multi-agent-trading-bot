"""Charts-first versus the real formula score (owner 2026-10-10): the comparison for the
review. Logging only; nothing here feeds a decision.

Per sleeve, over the decisions that carry both scores:
- how often the two scores call a different direction (a call is a score beyond ±CALL);
- the information coefficient of each against the next day's return;
- the mean next-day return in the direction each score called.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import DecisionRecord
from app.decision.tuning import _closes, _ic, forward_return

CALL = 0.10  # a score beyond this calls a direction


def call(score: float) -> int:
    return 1 if score >= CALL else -1 if score <= -CALL else 0


def summarize(rows: list[tuple[float, float, float | None]]) -> dict[str, Any]:
    """``rows``: (formula score, charts-first score, next-day return or None)."""
    n = len(rows)
    differ = sum(1 for f, c, _ in rows if call(f) != call(c))
    with_ret = [(f, c, r) for f, c, r in rows if r is not None]

    def called(idx: int) -> tuple[int, float | None]:
        hits = [call(row[idx]) * row[2] for row in with_ret if call(row[idx]) != 0]
        return len(hits), (sum(hits) / len(hits) if hits else None)

    f_n, f_ret = called(0)
    c_n, c_ret = called(1)
    return {
        "decisions": n,
        "calls_differ": differ,
        "calls_differ_share": differ / n if n else None,
        "with_return": len(with_ret),
        "ic_formula": _ic([(f, r) for f, _, r in with_ret]),
        "ic_charts_first": _ic([(c, r) for _, c, r in with_ret]),
        "formula_calls": f_n,
        "formula_called_return": f_ret,
        "charts_first_calls": c_n,
        "charts_first_called_return": c_ret,
    }


def compare(session: Session, cfg: Config, since: datetime) -> list[dict[str, Any]]:
    decs = session.scalars(
        select(DecisionRecord).where(
            DecisionRecord.ts >= since,
            DecisionRecord.formula_score.is_not(None),
            DecisionRecord.charts_first_score.is_not(None),
        )
    ).all()
    closes = {a: _closes(session, cfg, a, since) for a in cfg.trading.assets}
    groups: dict[str, list[tuple[float, float, float | None]]] = {}
    for d in decs:
        series = closes.get(d.asset)
        ret = forward_return(series, d.ts, 24) if series is not None else None
        row = (float(d.formula_score or 0), float(d.charts_first_score or 0), ret)
        groups.setdefault(cfg.sleeve_of(d.asset) or "none", []).append(row)
        groups.setdefault("all", []).append(row)
    order = [*cfg.sleeves, "all"]
    return [{"sleeve": k, **summarize(groups[k])} for k in order if k in groups]
