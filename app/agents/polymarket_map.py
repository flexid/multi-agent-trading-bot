"""Daily mapping of Polymarket markets to assets and thresholds (SPEC §6).

The only place a model reads Polymarket question text. Its output is a schema the code
validates and stores; the agent never sees the text again.

    python -m app.agents.polymarket_map          # map unmapped markets
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.config import get_config
from app.db.models import PolymarketMarket
from app.db.session import new_session
from app.llm import LLMError, complete, load_prompt

TASK = "polymarket"
BATCH = 40
ASSETS = {"BTC", "ETH", "SOL", "BNB", "SPX6900", "SP500", "OTHER"}


class Asset(StrEnum):
    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"
    BNB = "BNB"
    SPX6900 = "SPX6900"
    SP500 = "SP500"
    OTHER = "OTHER"


class Direction(StrEnum):
    ABOVE = "above"
    BELOW = "below"


class Kind(StrEnum):
    PRICE = "price"
    OTHER = "other"


class MarketMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    asset: Asset
    kind: Kind
    threshold: float | None = Field(default=None, gt=0)
    direction: Direction | None = None


class MappingBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markets: list[MarketMapping]


def _sane(m: MarketMapping) -> bool:
    if m.kind is Kind.PRICE:
        return m.threshold is not None and m.direction is not None and m.asset is not Asset.OTHER
    return True


async def map_unmapped(limit: int = 400) -> int:
    """Map markets without a mapping. Returns how many were stored."""
    prompt = load_prompt("polymarket_map")
    with new_session() as session:
        rows = session.execute(
            select(PolymarketMarket.id, PolymarketMarket.question)
            .where(PolymarketMarket.mapped_at.is_(None), PolymarketMarket.closed.is_(False))
            .limit(limit)
        ).all()
    stored = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        ids = {r.id for r in batch}
        payload = json.dumps([{"id": r.id, "question": r.question[:300]} for r in batch])
        try:
            result = await complete(TASK, MappingBatch, prompt=prompt, user_text=payload)
        except LLMError:
            continue  # logged by the LLM layer; these markets stay unmapped for the next run
        now = datetime.now(UTC)
        with new_session() as session:
            for m in result.parsed.markets:
                if m.id not in ids or not _sane(m):
                    continue
                market = session.get(PolymarketMarket, m.id)
                if market is None:
                    continue
                market.asset = m.asset.value if m.asset is not Asset.OTHER else None
                market.mapping = (
                    {"kind": "price", "threshold": m.threshold, "direction": m.direction}
                    if m.kind is Kind.PRICE
                    else {"kind": "other"}
                )
                market.mapped_at = now
                stored += 1
            session.commit()
    return stored


def main() -> int:
    cfg = get_config()
    n = asyncio.run(map_unmapped())
    print(f"mapped {n} markets with {cfg.models.polymarket}")
    with new_session() as session:
        for asset in cfg.trading.assets + ["SP500"]:
            count = session.execute(
                select(PolymarketMarket.id).where(PolymarketMarket.asset == asset)
            ).all()
            print(f"  {asset:8s} {len(count)} markets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
