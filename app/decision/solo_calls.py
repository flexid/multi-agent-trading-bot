"""Solo calls (owner 2026-10-11): what happened after exactly one manager called a
direction and the others stayed flat. One call alone never trades, so the others' silence
blocks it; this log shows whether that saved money or cost it. Logging only.

Per caller, over the managers' own seats (two until logic v5, three since v6):
- how many solo calls, the mean move in the called direction after 4 hours and after a
  day (before costs), and the share that went the caller's way;
- as a baseline, the same for the calls that became a trade (all agree, or two of three
  with nobody opposite);
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


Answers = list[tuple[str, str]]  # (model, direction) per manager that answered


def summarize(rows: list[tuple[Answers, float | None, float | None]]) -> dict[str, Any]:
    """``rows``: (the managers' answers, 4h return, 1d return) per decision, returns
    unsigned (the asset's move)."""
    solo: dict[str, dict[str, list[float]]] = {}
    traded: dict[str, list[float]] = {"h4": [], "d1": []}
    counts: dict[str, list[int]] = {}  # model -> [proposals, flat]
    n_solo = n_traded = n_opposite = 0
    for answers, r4, r1 in rows:
        for m, d in answers:
            c = counts.setdefault(m, [0, 0])
            c[0] += 1
            c[1] += d == "flat"
        called = [(m, d) for m, d in answers if d in SIGN]
        if len({d for _, d in called}) > 1:
            n_opposite += 1
            continue
        if len(called) >= 2:
            n_traded += 1
            side = SIGN[called[0][1]]
            if r4 is not None:
                traded["h4"].append(side * r4)
            if r1 is not None:
                traded["d1"].append(side * r1)
        elif len(called) == 1:
            n_solo += 1
            caller, direction = called[0]
            bucket = solo.setdefault(caller, {"h4": [], "d1": [], "n": []})
            bucket["n"].append(1.0)
            if r4 is not None:
                bucket["h4"].append(SIGN[direction] * r4)
            if r1 is not None:
                bucket["d1"].append(SIGN[direction] * r1)
    return {
        "decisions": len(rows),
        "solo": n_solo,
        "agreed": n_traded,
        "opposite": n_opposite,
        "pairs": [
            {"caller": caller, "calls": len(b["n"]), "h4": _stats(b["h4"]), "d1": _stats(b["d1"])}
            for caller, b in sorted(solo.items())
        ],
        "agree": {"calls": n_traded, "h4": _stats(traded["h4"]), "d1": _stats(traded["d1"])},
        "flat_share": {m: (c[1] / c[0] if c[0] else None) for m, c in sorted(counts.items())},
    }


def compare(session: Session, cfg: Config, since: datetime) -> dict[str, Any]:
    seated = {"pm_1": cfg.models.pm_1, "pm_2": cfg.models.pm_2}
    if cfg.models.pm_3:
        seated["pm_3"] = cfg.models.pm_3
    props = session.scalars(
        select(PMProposalRecord).where(PMProposalRecord.variant == "main")
    ).all()
    by_key: dict[tuple[int, str], dict[str, PMProposalRecord]] = {}
    for p in props:
        by_key.setdefault((p.cycle_id, p.asset), {})[p.pm] = p
    decs = session.scalars(select(DecisionRecord).where(DecisionRecord.ts >= since)).all()
    closes = {a: _closes(session, cfg, a, since) for a in cfg.trading.assets}
    rows: list[tuple[Answers, float | None, float | None]] = []
    for d in decs:
        seats = by_key.get((d.cycle_id, d.asset)) or {}
        answers = [
            (p.model, p.direction)
            for seat, p in sorted(seats.items())
            if seated.get(seat) == p.model  # a model that no longer holds the seat is left out
        ]
        if len(answers) < 2:
            continue
        series = closes.get(d.asset)
        r4 = forward_return(series, d.ts, 4) if series is not None else None
        r1 = forward_return(series, d.ts, 24) if series is not None else None
        rows.append((answers, r4, r1))
    return {"managers": len(seated), **summarize(rows)}
