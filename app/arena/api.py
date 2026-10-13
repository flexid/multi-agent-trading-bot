"""The Competition API (bot spec §6): read-only, percentages and the equity index only,
consumed by arena.dorkbot.dev. Mounted at /arena/v1; served by the arena process."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from app.arena import ledger
from app.arena.engine import Engine
from app.config import get_config
from app.db.session import new_session

PREFIX = "/arena/v1"


def build_app(engine: Engine | None) -> FastAPI:
    cfg = get_config().arena
    app = FastAPI(title="dorkbot arena", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["GET"], allow_headers=["*"]
    )

    @app.get(PREFIX + "/state")
    async def state() -> dict[str, Any]:
        now = datetime.now(UTC)
        positions: dict[str, dict[str, Any] | None] = {}
        if engine is not None:
            raw = {}
            for tid, t in engine.traders.items():
                try:
                    raw[tid] = await t.exchange.position()
                except Exception:
                    raw[tid] = None
            positions = engine.positions_public(raw, now)
        with new_session() as s:
            view = ledger.state_view(
                s, positions=positions, losses_to_bench=cfg.losses_to_bench, now=now
            )
        view["mode"] = "live" if cfg.live else "paper"
        return view

    @app.get(PREFIX + "/leaderboard")
    def leaderboard() -> dict[str, Any]:
        with new_session() as s:
            return ledger.leaderboard_view(s, datetime.now(UTC))

    @app.get(PREFIX + "/trades")
    def trades(
        trader: str | None = None, limit: int = Query(50, ge=1, le=500), before: int | None = None
    ) -> dict[str, Any]:
        with new_session() as s:
            return ledger.trades_view(s, trader=trader, limit=limit, before=before)

    @app.get(PREFIX + "/rounds")
    def rounds() -> dict[str, Any]:
        with new_session() as s:
            return ledger.rounds_view(s)

    @app.get(PREFIX + "/equity")
    def equity(
        trader: str | None = None, from_: str | None = Query(None, alias="from")
    ) -> dict[str, Any]:
        since = datetime.fromisoformat(from_) if from_ else None
        with new_session() as s:
            return ledger.equity_view(s, trader=trader, since=since)

    @app.get(PREFIX + "/decisions")
    def decisions(
        trader: str | None = None, limit: int = Query(30, ge=1, le=500)
    ) -> dict[str, Any]:
        with new_session() as s:
            return ledger.decisions_view(s, trader=trader, limit=limit)

    @app.get(PREFIX + "/rulebooks")
    def rulebooks(trader: str) -> dict[str, Any]:
        with new_session() as s:
            return ledger.rulebooks_view(s, trader)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "round": engine.round_number if engine else None}

    return app
