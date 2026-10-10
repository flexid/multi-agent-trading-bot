"""LLM and X cost per sleeve (owner 2026-10-10). Calls carry the asset they served; PM
calls and account reads serve the whole book and land in "shared"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import LLMCall, XPostRecord

X_READ_USD = Decimal("0.005")


@dataclass(frozen=True)
class SleeveCost:
    sleeve: str
    llm_usd: Decimal
    llm_calls: int
    x_reads: int

    @property
    def x_usd(self) -> Decimal:
        return self.x_reads * X_READ_USD

    @property
    def total(self) -> Decimal:
        return self.llm_usd + self.x_usd


def costs_by_sleeve(session: Session, cfg: Config, since: datetime) -> list[SleeveCost]:
    llm = session.execute(
        select(LLMCall.asset, func.count(), func.coalesce(func.sum(LLMCall.cost_usd), 0))
        .where(LLMCall.ts >= since)
        .group_by(LLMCall.asset)
    ).all()
    reads = session.execute(
        select(XPostRecord.query_asset, func.count())
        .where(XPostRecord.fetched_at >= since)
        .group_by(XPostRecord.query_asset)
    ).all()
    names = [*cfg.sleeves.keys(), "shared"]
    llm_usd = dict.fromkeys(names, Decimal(0))
    llm_n = dict.fromkeys(names, 0)
    x_n = dict.fromkeys(names, 0)
    for asset, n, usd in llm:
        key = cfg.sleeve_of(asset) if asset else None
        key = key or "shared"
        llm_usd[key] += Decimal(str(usd))
        llm_n[key] += int(n)
    for asset, n in reads:
        key = cfg.sleeve_of(asset) if asset else None
        key = key or "shared"
        x_n[key] += int(n)
    return [SleeveCost(name, llm_usd[name], llm_n[name], x_n[name]) for name in names]
