"""Three ways to turn the managers' answers into a trade, compared on what followed
(owner 2026-10-11). The live rule is two of three since logic v6; the other two are
counterfactuals, kept to judge the switch at the review.

For every decision where all three managers answered:
- **two of two** (the rule until v5): the first two managers call the same direction;
- **two of three** (live): at least two of the three call the same direction and nobody
  calls the opposite one (three agreeing is a full-size trade, two a half-size one);
- **score threshold**: the first two managers' own scores lie beyond ±``THRESHOLD`` on
  the same side, whatever direction they wrote.

Per rule: how many trades it would have opened, the mean move in that direction after
4 hours and a day (before costs) and the share that went its way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import DecisionRecord, PMProposalRecord
from app.decision.tuning import _closes, forward_return

THRESHOLD = 0.10
SIGN = {"long": 1, "short": -1, "flat": 0}


@dataclass(frozen=True)
class Seat:
    direction: str
    score: float


def two_of_two(a: Seat, b: Seat) -> int:
    sa, sb = SIGN[a.direction], SIGN[b.direction]
    return sa if sa != 0 and sa == sb else 0


def two_of_three(a: Seat, b: Seat, c: Seat) -> tuple[int, float]:
    """(direction, size): size 1.0 when all three agree, 0.5 when two do and the third is
    flat, nothing when anyone points the other way."""
    signs = [SIGN[s.direction] for s in (a, b, c)]
    if 1 in signs and -1 in signs:
        return 0, 0.0
    side = 1 if signs.count(1) >= 2 else -1 if signs.count(-1) >= 2 else 0
    if side == 0:
        return 0, 0.0
    return side, 1.0 if signs.count(side) == 3 else 0.5


def by_threshold(a: Seat, b: Seat) -> int:
    if a.score >= THRESHOLD and b.score >= THRESHOLD:
        return 1
    if a.score <= -THRESHOLD and b.score <= -THRESHOLD:
        return -1
    return 0


def _stats(moves: list[float]) -> dict[str, Any]:
    if not moves:
        return {"n": 0, "mean": None, "won": None}
    return {
        "n": len(moves),
        "mean": sum(moves) / len(moves),
        "won": sum(1 for m in moves if m > 0) / len(moves),
    }


def summarize(rows: list[tuple[Seat, Seat, Seat, float | None, float | None]]) -> dict[str, Any]:
    """``rows``: (seat 1, seat 2, shadow seat, 4h return, 1d return) per decision."""
    rules: dict[str, dict[str, list[float]]] = {
        k: {"n": [], "h4": [], "d1": []}
        for k in ("two_of_two", "two_of_three", "three_of_three", "threshold")
    }
    flat = [0, 0, 0]

    def add(rule: str, side: int, r4: float | None, r1: float | None) -> None:
        if side == 0:
            return
        rules[rule]["n"].append(1.0)
        if r4 is not None:
            rules[rule]["h4"].append(side * r4)
        if r1 is not None:
            rules[rule]["d1"].append(side * r1)

    for a, b, c, r4, r1 in rows:
        for i, s in enumerate((a, b, c)):
            flat[i] += s.direction == "flat"
        add("two_of_two", two_of_two(a, b), r4, r1)
        side, size = two_of_three(a, b, c)
        add("two_of_three", side, r4, r1)
        if size == 1.0:
            add("three_of_three", side, r4, r1)
        add("threshold", by_threshold(a, b), r4, r1)
    n = len(rows)
    return {
        "decisions": n,
        "threshold": THRESHOLD,
        "rules": {
            k: {"trades": len(v["n"]), "h4": _stats(v["h4"]), "d1": _stats(v["d1"])}
            for k, v in rules.items()
        },
        "flat_share": [f / n if n else None for f in flat],
    }


def compare(session: Session, cfg: Config, since: datetime) -> dict[str, Any]:
    third = cfg.models.pm_3
    models = [cfg.models.pm_1, cfg.models.pm_2, third]
    if not third:
        return {
            "decisions": 0,
            "models": models,
            "rules": {},
            "flat_share": [],
            "third_asked": 0,
            "third_answered": 0,
        }
    props = session.scalars(
        select(PMProposalRecord).where(PMProposalRecord.variant == "main")
    ).all()
    by_key: dict[tuple[int, str], dict[str, PMProposalRecord]] = {}
    for p in props:
        by_key.setdefault((p.cycle_id, p.asset), {})[p.pm] = p
    decs = session.scalars(select(DecisionRecord).where(DecisionRecord.ts >= since)).all()
    closes = {a: _closes(session, cfg, a, since) for a in cfg.trading.assets}
    rows: list[tuple[Seat, Seat, Seat, float | None, float | None]] = []
    asked = answered = 0  # since the third seat exists: did it answer in time?
    first_third = min((p.cycle_id for p in props if p.pm == "pm_3"), default=None)
    for d in decs:
        seats = by_key.get((d.cycle_id, d.asset)) or {}
        p1, p2, p3 = seats.get("pm_1"), seats.get("pm_2"), seats.get("pm_3")
        if first_third is not None and d.cycle_id >= first_third and (p1 or p2):
            asked += 1
            answered += p3 is not None
        if p1 is None or p2 is None or p3 is None:
            continue
        if [p1.model, p2.model, p3.model] != models:
            continue
        series = closes.get(d.asset)
        rows.append(
            (
                Seat(p1.direction, p1.score),
                Seat(p2.direction, p2.score),
                Seat(p3.direction, p3.score),
                forward_return(series, d.ts, 4) if series is not None else None,
                forward_return(series, d.ts, 24) if series is not None else None,
            )
        )
    return {
        "models": models,
        "third_asked": asked,
        "third_answered": answered,
        **summarize(rows),
    }
