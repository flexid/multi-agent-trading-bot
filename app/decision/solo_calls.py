"""Solo calls (owner 2026-10-11): what happened after one manager called a direction and
the other stayed flat. A trade needs both, so a flat manager is a veto; this log shows
whether those vetoes saved money or cost it. Logging only; nothing here feeds a decision.

Per pair (caller model, flat model), over the main seats:
- how many solo calls, the mean move in the called direction after 4 hours and after a
  day (before costs), and the share that went the caller's way;
- as a baseline, the same for the calls both managers agreed on;
- each model's share of flat proposals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import DecisionRecord, PMProposalRecord
from app.decision.tuning import _closes, forward_return

SIGN = {"long": 1.0, "short": -1.0}


def _stats(moves: list[float]) -> dict[str, Any]:
    if not moves:
        return {"n": 0, "mean": None, "won": None}
    return {
        "n": len(moves),
        "mean": sum(moves) / len(moves),
        "won": sum(1 for m in moves if m > 0) / len(moves),
    }


def summarize(
    rows: list[tuple[str, str, str, str, float | None, float | None]],
) -> dict[str, Any]:
    """``rows``: (model 1, direction 1, model 2, direction 2, 4h return, 1d return) per
    decision, returns unsigned (the asset's move)."""
    solo: dict[tuple[str, str], dict[str, list[float]]] = {}
    agree: dict[str, list[float]] = {"h4": [], "d1": []}
    counts: dict[str, list[int]] = {}  # model -> [proposals, flat]
    n_solo = n_agree = n_opposite = 0
    for m1, d1, m2, d2, r4, r1 in rows:
        for m, d in ((m1, d1), (m2, d2)):
            c = counts.setdefault(m, [0, 0])
            c[0] += 1
            c[1] += d == "flat"
        called = [(m, d, o) for m, d, o in ((m1, d1, m2), (m2, d2, m1)) if d in SIGN]
        if len(called) == 2:
            if d1 != d2:
                n_opposite += 1
                continue
            n_agree += 1
            if r4 is not None:
                agree["h4"].append(SIGN[d1] * r4)
            if r1 is not None:
                agree["d1"].append(SIGN[d1] * r1)
        elif len(called) == 1:
            n_solo += 1
            caller, direction, other = called[0]
            bucket = solo.setdefault((caller, other), {"h4": [], "d1": [], "n": []})
            bucket["n"].append(1.0)
            if r4 is not None:
                bucket["h4"].append(SIGN[direction] * r4)
            if r1 is not None:
                bucket["d1"].append(SIGN[direction] * r1)
    return {
        "decisions": len(rows),
        "solo": n_solo,
        "agreed": n_agree,
        "opposite": n_opposite,
        "pairs": [
            {
                "caller": caller,
                "flat": other,
                "calls": len(b["n"]),
                "h4": _stats(b["h4"]),
                "d1": _stats(b["d1"]),
            }
            for (caller, other), b in sorted(solo.items())
        ],
        "agree": {"calls": n_agree, "h4": _stats(agree["h4"]), "d1": _stats(agree["d1"])},
        "flat_share": {m: (c[1] / c[0] if c[0] else None) for m, c in sorted(counts.items())},
    }


def compare(session: Session, cfg: Config, since: datetime) -> dict[str, Any]:
    seated = {cfg.models.pm_1, cfg.models.pm_2}
    props = session.scalars(
        select(PMProposalRecord).where(PMProposalRecord.variant == "main")
    ).all()
    by_key: dict[tuple[int, str], dict[str, PMProposalRecord]] = {}
    for p in props:
        by_key.setdefault((p.cycle_id, p.asset), {})[p.pm] = p
    decs = session.scalars(select(DecisionRecord).where(DecisionRecord.ts >= since)).all()
    closes = {a: _closes(session, cfg, a, since) for a in cfg.trading.assets}
    rows: list[tuple[str, str, str, str, float | None, float | None]] = []
    for d in decs:
        pair = by_key.get((d.cycle_id, d.asset)) or {}
        p1, p2 = pair.get("pm_1"), pair.get("pm_2")
        if p1 is None or p2 is None or {p1.model, p2.model} != seated:
            continue  # a seat failed, or a model that no longer holds a seat
        series = closes.get(d.asset)
        r4 = forward_return(series, d.ts, 4) if series is not None else None
        r1 = forward_return(series, d.ts, 24) if series is not None else None
        rows.append((p1.model, p1.direction, p2.model, p2.direction, r4, r1))
    return summarize(rows)
