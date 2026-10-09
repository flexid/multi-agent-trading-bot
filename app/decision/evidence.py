"""Evidence pack (SPEC §7.1): what both PMs see, and nothing else.

Built only from validated agent outputs and the bot's own records. No raw X text, no
market questions, no model prose except the agents' evidence lines.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.schema import AgentOutput
from app.db.models import Candle, DecisionRecord

RECENT_DECISIONS = 10


class AgentView(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent: str
    score: float
    confidence: float
    horizon: str
    valid: bool
    evidence: list[str]
    risk_flags: list[str]


class PastDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    hours_ago: int
    direction: str
    consensus_score: float | None
    price_change_since_pct: float | None  # forward return of spot since the decision


class OpenPosition(BaseModel):
    model_config = ConfigDict(frozen=True)

    direction: str
    entry: float
    leverage: float
    stop: float
    target: float
    hours_open: float
    unrealized_pct: float


class AssetPack(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset: str
    spot: float
    atr_4h: float | None
    agents: list[AgentView]
    valid_agents: int
    macro_regime: str | None
    coupling: float | None
    open_position: OpenPosition | None
    recent_decisions: list[PastDecision]


class EvidencePack(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: datetime
    mode: str
    day_pnl_pct: float
    assets: list[AssetPack] = Field(min_length=1)


def spot_and_atr(session: Session, symbol: str) -> tuple[float, float | None]:
    rows = session.execute(
        select(Candle.high, Candle.low, Candle.close)
        .where(Candle.symbol == symbol, Candle.interval == "240")
        .order_by(Candle.open_time.desc())
        .limit(15)
    ).all()
    if not rows:
        return 0.0, None
    spot = float(rows[0].close)
    trs = [
        max(float(h) - float(lo), abs(float(h) - float(pc)), abs(float(lo) - float(pc)))
        for (h, lo, _), (_, _, pc) in zip(rows[:-1], rows[1:], strict=False)
    ]
    return spot, (sum(trs) / len(trs) if trs else None)


def recent_decisions(
    session: Session, asset: str, now: datetime, spot: float
) -> list[PastDecision]:
    rows = (
        session.execute(
            select(DecisionRecord)
            .where(DecisionRecord.asset == asset)
            .order_by(DecisionRecord.ts.desc())
            .limit(RECENT_DECISIONS)
        )
        .scalars()
        .all()
    )
    out = []
    for d in rows:
        change = (
            (spot / float(d.spot) - 1) * 100 if d.spot and float(d.spot) > 0 and spot > 0 else None
        )
        out.append(
            PastDecision(
                hours_ago=int((now - d.ts).total_seconds() // 3600),
                direction=d.direction,
                consensus_score=d.consensus_score,
                price_change_since_pct=round(change, 2) if change is not None else None,
            )
        )
    return out


def build_pack(
    session: Session,
    *,
    mode: str,
    outputs: dict[str, list[AgentOutput]],  # asset -> outputs
    symbols: dict[str, str],  # asset -> symbol
    macro_regime: str | None,
    couplings: dict[str, float],
    now: datetime | None = None,
) -> EvidencePack:
    now = now or datetime.now(UTC)
    assets = []
    for asset, outs in outputs.items():
        spot, atr = spot_and_atr(session, symbols[asset])
        views = [
            AgentView(
                agent=o.agent,
                score=round(o.score, 3),
                confidence=round(o.confidence, 3),
                horizon=o.horizon.value,
                valid=o.valid,
                evidence=o.evidence,
                risk_flags=o.risk_flags,
            )
            for o in outs
        ]
        assets.append(
            AssetPack(
                asset=asset,
                spot=spot,
                atr_4h=round(atr, 6) if atr else None,
                agents=views,
                valid_agents=sum(v.valid for v in views),
                macro_regime=macro_regime,
                coupling=couplings.get(asset),
                open_position=None,  # positions arrive with the executor (M6)
                recent_decisions=recent_decisions(session, asset, now, spot),
            )
        )
    return EvidencePack(as_of=now, mode=mode, day_pnl_pct=0.0, assets=assets)


def as_of_window(now: datetime) -> datetime:
    return now - timedelta(hours=4)


__all__ = ["AgentView", "AssetPack", "EvidencePack", "OpenPosition", "build_pack", "Decimal"]
