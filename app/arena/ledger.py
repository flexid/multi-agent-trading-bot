"""The arena's rows (bot spec §8) and the read side of the API (§6). Writes go through
the engine only; reads return percentages, counts and the equity index, never USD."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    ArenaBenchEvent,
    ArenaDecision,
    ArenaEquity,
    ArenaRound,
    ArenaRulebook,
    ArenaTrade,
    ArenaTrader,
)

LOSSES_TO_BENCH = 2  # the API's display default; the engine uses the config


def _f(x: Decimal | float | None, d: int = 2) -> float | None:
    return None if x is None else round(float(x), d)


# --- writes ----------------------------------------------------------------------------


def ensure_trader(s: Session, trader_id: str, name: str, model: str, now: datetime) -> ArenaTrader:
    row = s.get(ArenaTrader, trader_id)
    if row is None:
        row = ArenaTrader(id=trader_id, display_name=name, model=model, created_at=now)
        s.add(row)
    elif row.model != model:
        row.model = model
    s.flush()
    return row


def current_round(s: Session) -> ArenaRound | None:
    return s.scalars(
        select(ArenaRound).where(ArenaRound.ended_at.is_(None)).order_by(ArenaRound.number.desc())
    ).first()


def start_round(s: Session, now: datetime) -> ArenaRound:
    last = s.execute(select(func.max(ArenaRound.number))).scalar_one() or 0
    row = ArenaRound(number=last + 1, started_at=now)
    s.add(row)
    s.flush()
    return row


def end_round(s: Session, rnd: ArenaRound, now: datetime, winner: str | None) -> None:
    rnd.ended_at, rnd.winner = now, winner


def losses_in_round(s: Session, trader: str, round_number: int) -> int:
    return int(
        s.execute(
            select(func.count())
            .select_from(ArenaTrade)
            .where(
                ArenaTrade.trader == trader,
                ArenaTrade.round == round_number,
                ArenaTrade.status == "closed",
                ArenaTrade.net_roe_pct <= 0,
            )
        ).scalar_one()
    )


def benched_in_round(s: Session, trader: str, round_number: int) -> ArenaBenchEvent | None:
    return s.scalars(
        select(ArenaBenchEvent).where(
            ArenaBenchEvent.trader == trader, ArenaBenchEvent.round == round_number
        )
    ).first()


def open_trade(s: Session, trader: str) -> ArenaTrade | None:
    return s.scalars(
        select(ArenaTrade).where(ArenaTrade.trader == trader, ArenaTrade.status == "open")
    ).first()


def latest_rulebook(s: Session, trader: str, round_number: int) -> ArenaRulebook | None:
    return s.scalars(
        select(ArenaRulebook)
        .where(ArenaRulebook.trader == trader, ArenaRulebook.round == round_number)
        .order_by(ArenaRulebook.id.desc())
    ).first()


def equity_at(s: Session, trader: str, at: datetime) -> Decimal | None:
    row = s.execute(
        select(ArenaEquity.equity_usdt)
        .where(ArenaEquity.trader == trader, ArenaEquity.ts <= at)
        .order_by(ArenaEquity.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row


# --- reads for the API -----------------------------------------------------------------


def _traders(s: Session) -> list[ArenaTrader]:
    return list(s.scalars(select(ArenaTrader).order_by(ArenaTrader.created_at)).all())


def _last_equity(s: Session, trader: str) -> tuple[Decimal, Decimal] | None:
    row = s.execute(
        select(ArenaEquity.equity_usdt, ArenaEquity.equity_index)
        .where(ArenaEquity.trader == trader)
        .order_by(ArenaEquity.ts.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def state_view(
    s: Session, *, positions: dict[str, dict[str, Any] | None], losses_to_bench: int, now: datetime
) -> dict[str, Any]:
    rnd = current_round(s)
    traders = _traders(s)
    first = s.execute(select(func.min(ArenaRound.started_at))).scalar_one()
    out = []
    for t in traders:
        last = _last_equity(s, t.id)
        rb = latest_rulebook(s, t.id, rnd.number) if rnd else None
        bench = benched_in_round(s, t.id, rnd.number) if rnd else None
        losses = losses_in_round(s, t.id, rnd.number) if rnd else 0
        out.append(
            {
                "id": t.id,
                "name": t.display_name,
                "model": model_label(t.model),
                "benched": bench is not None,
                "benched_at": bench.at.isoformat() if bench else None,
                "losses": min(losses, losses_to_bench),
                "equity_index": _f(last[1]) if last else 100.0,
                "return_pct": _f(last[1] - 100) if last else 0.0,
                "position": positions.get(t.id),
                "rulebook": rb.rulebook if rb and rb.rulebook else None,  # {} = the author failed
                "last_decision": decision_public(ld) if (ld := last_decision(s, t.id)) else None,
                "judgments_this_round": decisions_in_round(s, t.id, rnd.number) if rnd else 0,
            }
        )
    return {
        "generated_at": now.isoformat(),
        "started_at": (first or now).isoformat(),
        "round": {
            "number": rnd.number if rnd else 0,
            "started_at": (rnd.started_at if rnd else now).isoformat(),
            "active": sum(1 for x in out if not x["benched"]),
        },
        "traders": out,
    }


def decision_public(d: ArenaDecision) -> dict[str, Any]:
    p = d.probabilities or {}
    return {
        "at": d.candle_ts.isoformat(),
        "kind": d.kind,
        "p_long": float(p.get("long", 0.0)),
        "p_short": float(p.get("short", 0.0)),
        "p_none": float(p.get("none", 0.0)),
        "p_close": float(p.get("true", 0.0)) if d.kind == "early_exit" else None,
        "conviction": float(d.conviction or 0.0),
        "min_conviction": float(d.min_conviction or 0.0),
        "acted": d.acted,
    }


def last_decision(s: Session, trader: str) -> ArenaDecision | None:
    return s.scalars(
        select(ArenaDecision)
        .where(ArenaDecision.trader == trader)
        .order_by(ArenaDecision.id.desc())
    ).first()


def decisions_in_round(s: Session, trader: str, round_number: int) -> int:
    return int(
        s.execute(
            select(func.count())
            .select_from(ArenaDecision)
            .where(ArenaDecision.trader == trader, ArenaDecision.round == round_number)
        ).scalar_one()
    )


def decisions_view(s: Session, *, trader: str | None, limit: int) -> dict[str, Any]:
    q = select(ArenaDecision)
    if trader:
        q = q.where(ArenaDecision.trader == trader)
    rows = s.scalars(q.order_by(ArenaDecision.id.desc()).limit(max(1, min(limit, 500)))).all()
    return {
        "decisions": [{"trader": d.trader, "round": d.round, **decision_public(d)} for d in rows]
    }


def model_label(model: str) -> str:
    m = model.lower()
    if m.startswith("gpt"):
        return "astra"
    if m.startswith("grok"):
        return "grok"
    if m.startswith("claude"):
        return "fable"
    return model


def leaderboard_view(s: Session, now: datetime) -> dict[str, Any]:
    out = []
    rounds_done = list(s.scalars(select(ArenaRound).where(ArenaRound.ended_at.is_not(None))).all())
    for t in _traders(s):
        closed = list(
            s.scalars(
                select(ArenaTrade)
                .where(ArenaTrade.trader == t.id, ArenaTrade.status == "closed")
                .order_by(ArenaTrade.closed_at)
            ).all()
        )
        rets = [float(x.net_roe_pct or 0) for x in closed]
        wins = sum(1 for r in rets if r > 0)
        streak = 0
        for r in reversed(rets):
            sign = 1 if r > 0 else -1
            if streak == 0:
                streak = sign
            elif (streak > 0) == (sign > 0):
                streak += sign
            else:
                break
        holds = [((x.closed_at or now) - x.opened_at).total_seconds() / 60 for x in closed]
        benched_rounds = {
            b.round
            for b in s.scalars(select(ArenaBenchEvent).where(ArenaBenchEvent.trader == t.id)).all()
        }
        survived = sum(1 for r in rounds_done if r.number not in benched_rounds)
        last = _last_equity(s, t.id)
        out.append(
            {
                "id": t.id,
                "name": t.display_name,
                "model": model_label(t.model),
                "equity_index": _f(last[1]) if last else 100.0,
                "return_pct": _f(last[1] - 100) if last else 0.0,
                "trades": len(closed),
                "wins": wins,
                "losses": len(closed) - wins,
                "win_rate_pct": _f(wins / len(closed) * 100, 1) if closed else None,
                "avg_net_roe_pct": _f(sum(rets) / len(rets)) if rets else None,
                "best_pct": _f(max(rets)) if rets else None,
                "worst_pct": _f(min(rets)) if rets else None,
                "rounds_survived": survived,
                "streak": streak,
                "avg_leverage": _f(sum(x.leverage for x in closed) / len(closed), 1)
                if closed
                else None,
                "avg_hold_min": _f(sum(holds) / len(holds), 0) if holds else None,
            }
        )
    return {"generated_at": now.isoformat(), "traders": out}


def trade_view(x: ArenaTrade, decision: ArenaDecision | None) -> dict[str, Any]:
    probs = (decision.probabilities if decision else {}) or {}
    return {
        "id": x.id,
        "trader": x.trader,
        "round": x.round,
        "side": x.side,
        "leverage": x.leverage,
        "entry": float(x.entry_price),
        "exit": _f(x.exit_price, 4),
        "tp": float(x.tp_price),
        "sl": float(x.sl_price),
        "opened_at": x.opened_at.isoformat(),
        "closed_at": x.closed_at.isoformat() if x.closed_at else None,
        "net_roe_pct": _f(x.net_roe_pct),
        "fees_pct_of_margin": _f(x.fees_usdt / x.margin_usdt * 100)
        if x.fees_usdt is not None and x.margin_usdt
        else None,
        "result": None if x.net_roe_pct is None else ("win" if x.net_roe_pct > 0 else "loss"),
        "exit_reason": x.exit_reason,
        "decision": {
            "p_long": float(probs.get("long", 0.0)),
            "p_short": float(probs.get("short", 0.0)),
            "p_none": float(probs.get("none", 0.0)),
            "conviction": float(decision.conviction or 0.0) if decision else 0.0,
            "min_conviction": float(decision.min_conviction or 0.0) if decision else 0.0,
        },
    }


def trades_view(
    s: Session, *, trader: str | None, limit: int, before: int | None
) -> dict[str, Any]:
    q = select(ArenaTrade).where(ArenaTrade.status == "closed")
    if trader:
        q = q.where(ArenaTrade.trader == trader)
    if before:
        q = q.where(ArenaTrade.id < before)
    rows = list(s.scalars(q.order_by(ArenaTrade.id.desc()).limit(max(1, min(limit, 500)))).all())
    ids = [r.decision_id for r in rows if r.decision_id]
    decisions = (
        {d.id: d for d in s.scalars(select(ArenaDecision).where(ArenaDecision.id.in_(ids))).all()}
        if ids
        else {}
    )
    return {"trades": [trade_view(r, decisions.get(r.decision_id or -1)) for r in rows]}


def rounds_view(s: Session) -> dict[str, Any]:
    rounds = list(s.scalars(select(ArenaRound).order_by(ArenaRound.number)).all())
    benches: dict[int, list[ArenaBenchEvent]] = defaultdict(list)
    for b in s.scalars(select(ArenaBenchEvent).order_by(ArenaBenchEvent.at)).all():
        benches[b.round].append(b)
    return {
        "rounds": [
            {
                "number": r.number,
                "started_at": r.started_at.isoformat(),
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "benches": [
                    {"trader": b.trader, "at": b.at.isoformat(), "losses": b.loss_count}
                    for b in benches[r.number]
                ],
                "winner": r.winner,
            }
            for r in rounds
        ]
    }


def equity_view(s: Session, *, trader: str | None, since: datetime | None) -> dict[str, Any]:
    q = select(ArenaEquity.trader, ArenaEquity.ts, ArenaEquity.equity_index)
    if trader:
        q = q.where(ArenaEquity.trader == trader)
    if since:
        q = q.where(ArenaEquity.ts >= since)
    series: dict[str, list[list[float]]] = defaultdict(list)
    for t, ts, idx in s.execute(q.order_by(ArenaEquity.ts)).all():
        series[t].append([int(ts.timestamp() * 1000), float(idx)])
    thinned = {k: _thin(v, 600) for k, v in series.items()}
    rounds = [
        {"number": r.number, "started_at": r.started_at.isoformat()}
        for r in s.scalars(select(ArenaRound).order_by(ArenaRound.number)).all()
    ]
    return {"rounds": rounds, "series": thinned}


def _thin(points: list[list[float]], n: int) -> list[list[float]]:
    if len(points) <= n:
        return points
    step = len(points) / n
    out = [points[int(i * step)] for i in range(n)]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def rulebooks_view(s: Session, trader: str) -> dict[str, Any]:
    rows = s.scalars(
        select(ArenaRulebook)
        .where(ArenaRulebook.trader == trader)
        .order_by(ArenaRulebook.round.desc(), ArenaRulebook.id.desc())
    ).all()
    seen: set[int] = set()
    out = []
    for r in rows:
        if r.round in seen or not r.rulebook:
            continue
        seen.add(r.round)
        out.append({"round": r.round, "rulebook": r.rulebook})
    return {"rulebooks": out}


def utcnow() -> datetime:
    return datetime.now(UTC)
