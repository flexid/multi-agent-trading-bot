"""dex.json for dex.dorkbot.dev: the mode as a clear label, the equity index, open and closed
mirror trades with their divergence from the main book, explorer links. Percentages and the
index only, never USD; the same whitelist as the main snapshot."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.config import Config
from app.db.models import DexEquity, DexState, DexTrade, Position
from app.db.session import new_session
from app.dex.exchange import DexExchange
from app.site.publish import push_r2
from app.site.snapshot import verify_dict

log = logging.getLogger("dex.snapshot")


def _f(x: Decimal | float | None, d: int = 2) -> float | None:
    return None if x is None else round(float(x), d)


async def build(
    cfg: Config, ex: DexExchange, mode: str, address: str, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    mids = await ex.mids()
    with new_session() as s:
        st = s.get(DexState, 1)
        last = s.execute(
            select(DexEquity).where(DexEquity.mode == mode).order_by(DexEquity.ts.desc()).limit(1)
        ).scalar_one_or_none()
        curve = [
            [int(r.ts.timestamp() * 1000), float(r.equity_index)]
            for r in s.scalars(
                select(DexEquity)
                .where(DexEquity.ts >= now - timedelta(days=30))
                .order_by(DexEquity.ts)
            ).all()
        ]
        open_rows = s.scalars(
            select(DexTrade)
            .where(DexTrade.status == "open", DexTrade.mode == mode)
            .order_by(DexTrade.opened_at)
        ).all()
        since = cfg.dex.record_since
        closed_q = select(DexTrade).where(DexTrade.status == "closed", DexTrade.mode == mode)
        if since is not None:  # the public record: trades the mirror opened after the line
            closed_q = closed_q.where(DexTrade.opened_at >= since)
            open_rows = [r for r in open_rows if r.opened_at >= since]
        all_closed = s.scalars(closed_q.order_by(DexTrade.closed_at)).all()
        closed_rows = list(reversed(all_closed))[:200]
        main_ids = [r.main_position_id for r in [*open_rows, *closed_rows]]
        mains = (
            {p.id: p for p in s.scalars(select(Position).where(Position.id.in_(main_ids))).all()}
            if main_ids
            else {}
        )
        opens = []
        unrealised = Decimal(0)
        for r in open_rows:
            px = mids.get(r.asset)
            move = (float(px / r.entry_price - 1) if px else 0.0) * (1 if r.side == "long" else -1)
            unrealised += Decimal(str(move)) * r.leverage * r.margin_usdt
            opens.append(
                {
                    "asset": r.asset,
                    "side": r.side,
                    "leverage": r.leverage,
                    "entry": float(r.entry_price),
                    "price": _f(px, 6),
                    "tp": float(r.tp_price),
                    "sl": float(r.sl_price),
                    "opened_at": r.opened_at.isoformat(),
                    "unrealized_price_pct": round(move * 100, 2),
                    "unrealized_margin_pct": round(move * r.leverage * 100, 2),
                    "main_entry": float(r.main_entry),
                    "entry_divergence_pct": round(float(r.entry_price / r.main_entry - 1) * 100, 3)
                    if r.main_entry
                    else None,
                }
            )
        closed = []
        for r in closed_rows:
            m = mains.get(r.main_position_id)
            closed.append(
                {
                    "asset": r.asset,
                    "side": r.side,
                    "leverage": r.leverage,
                    "entry": float(r.entry_price),
                    "exit": _f(r.exit_price, 6),
                    "opened_at": r.opened_at.isoformat(),
                    "closed_at": r.closed_at.isoformat() if r.closed_at else None,
                    "net_roe_pct": _f(r.net_roe_pct),
                    "exit_reason": r.exit_reason,
                    "main_margin_pct": _f(Decimal(str(m.pnl_margin_pct)) * 100)
                    if m is not None and m.pnl_margin_pct is not None
                    else None,
                    "main_entry": float(r.main_entry),
                    "entry_divergence_pct": round(float(r.entry_price / r.main_entry - 1) * 100, 3)
                    if r.main_entry
                    else None,
                    "tx": r.tx,
                }
            )
        if since is not None:
            # the index moves with the record's trades only: 100 at the line, each close a
            # step, the open trades' unrealised result as the last point
            base = (st.start_equity if st and st.start_equity else None) or cfg.dex.paper_equity
            index, curve = Decimal(100), [[int(since.timestamp() * 1000), 100.0]]
            for r in all_closed:
                index += (r.pnl_usdt or Decimal(0)) / base * 100
                curve.append([int((r.closed_at or now).timestamp() * 1000), float(round(index, 2))])
            index += unrealised / base * 100
            curve.append([int(now.timestamp() * 1000), float(round(index, 2))])
            index_now = float(round(index, 2))
        else:
            index_now = float(round(last.equity_index, 2)) if last else 100.0
    return {
        "generated_at": now.isoformat(),
        "mode": mode,
        "venue": "hyperliquid",
        "address": address if mode != "paper" else None,
        "explorer": f"{cfg.dex.explorer}/address/{address}"
        if address and mode != "paper"
        else None,
        "follows": cfg.dex.follow_track,
        "since": since.isoformat()
        if since is not None
        else (st.started_at.isoformat() if st and st.started_at else None),
        "record_note": cfg.dex.record_note or None,
        "equity_index": index_now,
        "return_pct": round(index_now - 100, 2),
        "mismatch": st.mismatch if st else None,
        "equity_curve": curve,
        "open_trades": opens,
        "closed_trades": closed,
        "stats": {
            "trades": len(all_closed),
            "wins": sum(1 for r in all_closed if (r.net_roe_pct or 0) > 0),
        },
    }


async def publish(cfg: Config, ex: DexExchange, mode: str, address: str) -> None:
    data = await build(cfg, ex, mode, address)
    problems = verify_dict(data)
    if problems:
        log.error("dex snapshot failed the whitelist: %s", problems[:3])
        return
    body = json.dumps(data).encode()
    if not push_r2({"dex.json": (body, "application/json")}, max_age=10):
        log.info("dex snapshot: no R2 credentials, not pushed")
