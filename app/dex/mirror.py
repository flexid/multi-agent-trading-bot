"""The mirror loop: follow the main book's record on the DEX, every few seconds.

For every open position on the followed track with no mirror trade yet: open the same side
at the plan's leverage, sized by the same margin share of the DEX equity, with the take and
the stop at the main book's distances from the DEX fill. For every mirror trade whose main
position has closed: close it, with the main book's reason. The DEX's own trigger orders are
the safety net; when they fire first the fill is booked as tp or sl. Positions are read back
from the venue every pass; a mismatch with the ledger alerts and blocks new entries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.config import Config
from app.db.models import DexEquity, DexState, DexTrade, Position
from app.db.session import new_session
from app.dex.exchange import DexExchange, Fill, round_size
from app.dex.paper import PaperDex


def close_cloid(open_cloid: str) -> str:
    """The close order's id: the open's 16-byte id with its low bit flipped, so it is
    distinct, idempotent per trade and still a valid venue id. ("<cloid>-x" was not: the
    live venue rejected every mirrored close with "cloid is not 16 bytes", 2026-10-21.)"""
    return f"0x{int(open_cloid, 16) ^ 1:032x}"


log = logging.getLogger("dex")
SHARE = {"bluechips": Decimal("0.14"), "alts": Decimal("0.06")}  # of equity, as the main book


@dataclass
class Mirror:
    cfg: Config
    ex: DexExchange
    mode: str
    start_equity: Decimal | None = None
    last_fill_check: datetime | None = None
    mismatch: str | None = None

    # --- lifecycle -------------------------------------------------------------------

    async def start(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        with new_session() as s:
            st = s.get(DexState, 1)
            if st is None:
                st = DexState(id=1, started_at=now, mode=self.mode)
                s.add(st)
            if st.mode != self.mode:
                # a new mode is a new record: fresh start, fresh index; old rows keep their mode
                log.info("dex: mode %s -> %s, new record", st.mode, self.mode)
                st.mode, st.started_at, st.start_equity, st.last_fill_check, st.mismatch = (
                    self.mode,
                    now,
                    None,
                    now,
                    None,
                )
            self.start_equity, self.last_fill_check = st.start_equity, st.last_fill_check or now
            if isinstance(self.ex, PaperDex):
                self._restore_paper(s)
            s.commit()
        log.info("dex: %s mirror of the %s book", self.mode, self.cfg.dex.follow_track)

    def _restore_paper(self, s: Any) -> None:
        assert isinstance(self.ex, PaperDex)
        from sqlalchemy import func as fn

        from app.dex.exchange import Position as DexPos

        realized = Decimal(
            str(
                s.execute(
                    select(fn.coalesce(fn.sum(DexTrade.pnl_usdt), 0)).where(
                        DexTrade.status == "closed"
                    )
                ).scalar_one()
            )
        )
        cash = (self.start_equity or self.cfg.dex.paper_equity) + realized
        positions = {}
        for row in s.scalars(
            select(DexTrade).where(DexTrade.status == "open", DexTrade.mode == self.mode)
        ).all():
            fee = row.entry_price * row.qty * self.ex.fee
            cash -= row.margin_usdt + fee
            positions[row.asset] = (
                DexPos(
                    row.asset,
                    row.side,
                    row.qty,
                    row.entry_price,
                    row.leverage,
                    Decimal(0),
                    row.margin_usdt,
                    None,
                ),
                row.tp_price,
                row.sl_price,
                fee,
            )
        self.ex.restore(cash, positions)
        if positions:
            log.info("dex: paper positions restored: %s", ", ".join(positions))

    # --- one pass ----------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if isinstance(self.ex, PaperDex):
            self.ex.now = now
            await self.ex.on_prices()
        await self.book_fills(now)
        await self.reconcile(now)
        await self.follow(now)
        await self.record_equity(now)

    async def book_fills(self, now: datetime) -> None:
        """Closes the venue reports since the last check: the trigger orders, or our own."""
        since = self.last_fill_check or now
        fills = await self.ex.closing_fills(since)
        if not fills:
            self.last_fill_check = now
            return
        with new_session() as s:
            for f in fills:
                row = s.scalars(
                    select(DexTrade).where(
                        DexTrade.asset == f.asset,
                        DexTrade.status == "open",
                        DexTrade.mode == self.mode,
                    )
                ).first()
                if row is None:
                    continue
                self._close_row(row, f, reason=None)
            st = s.get(DexState, 1)
            if st is not None:
                st.last_fill_check = now
            s.commit()
        self.last_fill_check = now

    def _close_row(self, row: DexTrade, f: Fill, reason: str | None) -> None:
        row.exit_price, row.closed_at, row.fees_usdt, row.pnl_usdt, row.tx = (
            f.price,
            f.at,
            f.fee,
            f.pnl,
            f.tx,
        )
        row.net_roe_pct = (
            (f.pnl / row.margin_usdt * 100).quantize(Decimal("0.01"))
            if row.margin_usdt
            else Decimal(0)
        )
        if reason is None:
            near_tp = abs(f.price - row.tp_price) <= abs(f.price - row.sl_price)
            reason = "tp" if near_tp else "sl"
        row.exit_reason, row.status = reason, "closed"
        log.info(
            "dex: closed %s %s at %s, net %s%% (%s)",
            row.asset,
            row.side,
            f.price,
            row.net_roe_pct,
            reason,
        )

    async def reconcile(self, now: datetime) -> None:
        """The venue's positions against the ledger. A difference blocks new entries."""
        venue = await self.ex.positions()
        with new_session() as s:
            ledger = {
                r.asset: r
                for r in s.scalars(
                    select(DexTrade).where(DexTrade.status == "open", DexTrade.mode == self.mode)
                ).all()
            }
            problems = []
            for asset in venue:
                if asset not in ledger:
                    problems.append(f"{asset} open on the venue, not in the ledger")
            for asset, r in ledger.items():
                # closed on the venue without a fill we saw yet: wait one more pass
                if asset not in venue and (now - r.opened_at).total_seconds() > 30:
                    problems.append(f"{asset} in the ledger, not on the venue")
            self.mismatch = "; ".join(problems) or None
            st = s.get(DexState, 1)
            if st is not None:
                st.mismatch = self.mismatch
            s.commit()
        if self.mismatch:
            log.warning("dex: mismatch, nothing new opens: %s", self.mismatch)

    async def follow(self, now: datetime) -> None:
        track = self.cfg.dex.follow_track
        instruments = await self.ex.instruments()
        allowed = set(self.cfg.dex.assets) or set(instruments)
        with new_session() as s:
            main_open = {
                p.asset: p
                for p in s.scalars(
                    select(Position).where(
                        Position.track == track, Position.status.in_(["open", "closing"])
                    )
                ).all()
            }
            mirrored = {
                r.asset: r
                for r in s.scalars(
                    select(DexTrade).where(DexTrade.status == "open", DexTrade.mode == self.mode)
                ).all()
            }
            # closes first: the main book left a trade the mirror still holds
            for asset, r in mirrored.items():
                main = s.get(Position, r.main_position_id)
                if main is not None and main.status == "closed":
                    try:
                        await self.ex.close_market(asset, close_cloid(r.cloid))
                    except Exception as exc:
                        log.error("dex: close %s failed: %s", asset, exc)
                        continue
                    fills = await self.ex.closing_fills(r.opened_at)
                    f = next((x for x in reversed(fills) if x.asset == asset), None)
                    if f is not None:
                        self._close_row(r, f, reason=f"mirror:{main.close_reason or 'closed'}"[:24])
            s.commit()
            if self.mismatch:
                return
            for asset, main in main_open.items():
                if asset in mirrored or asset not in allowed or asset not in instruments:
                    continue
                if s.scalars(
                    select(DexTrade).where(
                        DexTrade.main_position_id == main.id, DexTrade.mode == self.mode
                    )
                ).first():
                    continue  # mirrored before and already closed on the venue
                await self.open_like(s, main, instruments[asset], now)
            s.commit()

    async def open_like(self, s: Any, main: Position, inst: Any, now: datetime) -> None:
        equity, _ = await self.ex.equity()
        if equity <= 0:
            return
        sleeve = self.cfg.sleeve_of(main.asset) or "bluechips"
        margin = equity * SHARE.get(sleeve, SHARE["bluechips"])
        if self.cfg.dex.max_margin_usdt > 0:
            margin = min(margin, self.cfg.dex.max_margin_usdt)
        leverage = max(1, min(int(main.leverage), self.cfg.dex.leverage_max, inst.max_leverage))
        if margin * leverage < self.cfg.dex.min_notional_usdt:
            log.info(
                "dex: %s skipped, %s USDT at %dx is under the venue minimum",
                main.asset,
                margin,
                leverage,
            )
            return
        mids = await self.ex.mids()
        px = mids.get(main.asset)
        if px is None or px <= 0 or main.entry_price is None:
            return
        from app.dex.exchange import UNIT

        unit = UNIT.get(main.asset, Decimal(1))
        qty = round_size(margin * leverage / (px * unit), inst.sz_decimals)
        if qty <= 0:
            log.warning("dex: %s: size rounds to zero at %s USDT", main.asset, margin)
            return
        # the main book's stop and target as distances from its fill, applied to ours
        stop_d = (main.hard_stop or main.stop) / main.entry_price
        target_d = main.target / main.entry_price
        tp, sl = (px * target_d), (px * stop_d)
        cloid = f"0x{int(now.timestamp() * 1000):024x}{main.id:08x}"[:34]
        try:
            await self.ex.set_leverage(main.asset, leverage)
            await self.ex.open_market(
                main.asset,
                main.direction,
                qty,
                tp if self.cfg.dex.venue_take else None,  # the target is kept for the record
                sl,
                cloid,
            )
        except Exception as exc:
            log.error("dex: open %s failed: %s", main.asset, exc)
            return
        pos = (await self.ex.positions()).get(main.asset)
        entry = pos.entry if pos is not None else px
        s.add(
            DexTrade(
                asset=main.asset,
                side=main.direction,
                leverage=leverage,
                qty=pos.qty if pos is not None else qty,
                entry_price=entry,
                tp_price=tp,
                sl_price=sl,
                margin_usdt=pos.margin
                if pos is not None and pos.margin > 0
                else entry * unit * qty / leverage,
                main_position_id=main.id,
                main_entry=main.entry_price,
                opened_at=now,
                cloid=cloid,
                status="open",
                mode=self.mode,
            )
        )
        log.info(
            "dex: opened %s %s %s at %s, %dx (main book at %s)",
            main.asset,
            main.direction,
            qty,
            entry,
            leverage,
            main.entry_price,
        )

    async def record_equity(self, now: datetime) -> None:
        try:
            equity, _ = await self.ex.equity()
        except Exception as exc:
            log.warning("dex: equity unavailable: %s", exc)
            return
        with new_session() as s:
            st = s.get(DexState, 1)
            if st is not None and st.start_equity is None and equity > 0:
                st.start_equity = equity
                self.start_equity = equity
            if self.start_equity:
                s.merge(
                    DexEquity(
                        ts=now,
                        equity_usdt=equity,
                        equity_index=(equity / self.start_equity * 100).quantize(Decimal("0.0001")),
                        mode=self.mode,
                    )
                )
            s.commit()
