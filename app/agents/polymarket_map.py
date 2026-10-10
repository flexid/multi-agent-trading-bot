"""Incremental mapping of Polymarket markets to assets and thresholds (SPEC §6).

Runs every 15 minutes and at startup. The deterministic parser
(``app/agents/polymarket_parse.py``) handles the templated crypto questions; only what
it cannot parse but does mention one of our assets goes to the model. The model's output
is a schema the code validates and stores; the agent never sees the text again.

    python -m app.agents.polymarket_map          # map unmapped markets
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agents.polymarket_parse import parse
from app.config import get_config
from app.db.models import PolymarketMarket
from app.db.session import new_session
from app.llm import LLMError, complete, load_prompt

log = logging.getLogger(__name__)

TASK = "polymarket"
BATCH = 40
ASSETS = {
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "DOGE",
    "PEPE",
    "HBAR",
    "PUMP",
    "ENA",
    "BNB",
    "SPX6900",
    "SP500",
    "OTHER",
}


class Asset(StrEnum):
    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"
    XRP = "XRP"
    DOGE = "DOGE"
    PEPE = "PEPE"
    HBAR = "HBAR"
    PUMP = "PUMP"
    ENA = "ENA"
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


def map_parsed() -> tuple[int, list[tuple[str, str]]]:
    """Parser pass over every unmapped open market. Returns (stored, leftovers for the
    model as (id, question))."""
    now = datetime.now(UTC)
    leftovers: list[tuple[str, str]] = []
    stored = 0
    with new_session() as session:
        rows = session.scalars(
            select(PolymarketMarket).where(
                PolymarketMarket.mapped_at.is_(None), PolymarketMarket.closed.is_(False)
            )
        ).all()
        for market in rows:
            parsed = parse(market.question, market.slug)
            if parsed is None:
                leftovers.append((market.id, market.question))
                continue
            market.asset = parsed.asset
            market.mapping = parsed.mapping()
            market.mapped_at = now
            stored += 1
        session.commit()
    return stored, leftovers


async def map_with_model(rows: list[tuple[str, str]]) -> int:
    prompt = load_prompt("polymarket_map")
    stored = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        ids = {r[0] for r in batch}
        payload = json.dumps([{"id": r[0], "question": r[1][:300]} for r in batch])
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
                    {
                        "kind": "price",
                        "threshold": m.threshold,
                        "direction": m.direction,
                        "source": "model",
                    }
                    if m.kind is Kind.PRICE
                    else {"kind": "other", "source": "model"}
                )
                market.mapped_at = now
                stored += 1
            session.commit()
    return stored


async def map_unmapped(limit: int = 400) -> int:
    """Map markets without a mapping: parser first, the model for the rest. Returns how
    many were stored."""
    parsed, leftovers = map_parsed()
    modelled = await map_with_model(leftovers[:limit]) if leftovers else 0
    log.info("polymarket mapper: %d parsed, %d by model, %d left", parsed, modelled, len(leftovers))
    return parsed + modelled


def main() -> int:
    cfg = get_config()
    n = asyncio.run(map_unmapped())
    print(f"mapped {n} markets (parser first, {cfg.models.polymarket} for the rest)")
    with new_session() as session:
        for asset in cfg.trading.assets + ["SP500"]:
            count = session.execute(
                select(PolymarketMarket.id).where(PolymarketMarket.asset == asset)
            ).all()
            print(f"  {asset:8s} {len(count)} markets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
