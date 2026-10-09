"""Executor process (SPEC §9): the only component that opens or closes positions.

    python -m app.execution.executor            # run forever
    python -m app.execution.executor --once     # one pass, then exit

Every ``TICK_S`` seconds it: refreshes quotes, applies control requests (kill, pause,
resume), closes positions whose stop, target, trailing stop, time-stop or liquidation
hit, opens positions for decisions with ``action = "open"`` that have no position yet,
accrues interest, writes the paper ledger and an equity snapshot, and heartbeats.

Mode is checked here, by this process, from config: ``live`` needs ``live_allowed``
and the go-live checker's verdict (M9); until then every order is paper, whatever the
decision row says. Live orders go through ``BybitClient`` with an ``orderLinkId`` and
are reconciled against the exchange every pass.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config, Secrets, get_config, get_secrets
from app.db.models import (
    ControlRequest,
    DecisionRecord,
    EquitySnapshot,
    Heartbeat,
    PaperAccount,
    Position,
    RiskState,
)
from app.db.session import new_session
from app.execution import simulator as sim
from app.execution.bybit_client import BybitClient
from app.execution.gateway import Gateway, Outcome, PaperGateway
from app.execution.simulator import ExitReason, Quote, Side
from app.social import poster, style

log = logging.getLogger("executor")
TICK_S = 10
TAKER_FEE = {"USDT": Decimal("0.001"), "USDC": Decimal("0.0005")}
QTY_STEP_FALLBACK = Decimal("0.000001")
HOURLY_BORROW_FALLBACK = Decimal("0.000005")


class Executor:
    def __init__(
        self,
        cfg: Config,
        secrets: Secrets,
        gateway: Gateway | None = None,
        *,
        feed: bool = True,
    ) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.mode = "paper"  # "live" only when this process decides so (see docstring)
        self.gateway: Gateway = gateway or PaperGateway(
            TAKER_FEE.get(cfg.exchange.quote, Decimal("0.001"))
        )
        self.feed = feed  # False in tests: quotes are injected
        self.quotes: dict[str, Quote] = {}
        self.quotes_stale = False
        self.qty_steps: dict[str, Decimal] = {}
        self.borrow_rates: dict[str, Decimal] = {}
        self.collateral_ratios: dict[str, Decimal] = {}
        self.frozen = False  # after a kill switch: no new positions until resumed
        self.style = style.load()
        self._client: BybitClient | None = None

    # --- market data ------------------------------------------------------------

    async def client(self) -> BybitClient:
        if self._client is None:
            self._client = BybitClient(
                self.secrets.bybit_base_url,
                self.secrets.bybit_api_key,
                self.secrets.bybit_api_secret,
                allow_orders=self.mode == "live",
            )
        return self._client

    async def refresh_quotes(self) -> None:
        """Pull quotes; on failure keep the last ones and mark them stale. Stops stay
        armed on the last known quote and fire on the next fresh one (feed-drop rule)."""
        if not self.feed:
            return
        client = await self.client()
        now = datetime.now(UTC)
        failures = 0
        for asset in self.cfg.trading.assets:
            symbol = self.cfg.symbol(asset)
            try:
                t = await client.ticker(symbol)
            except Exception as exc:
                failures += 1
                log.warning("ticker %s failed: %s", symbol, exc)
                continue
            self.quotes[symbol] = Quote(t.bid1_price, t.ask1_price, now)
            if symbol not in self.qty_steps:
                inst = await client.instrument(symbol)
                if inst is not None:
                    self.qty_steps[symbol] = inst.lot_size_filter.base_precision
                margin = await client.margin_coin(inst.base_coin if inst else asset)
                self.borrow_rates[symbol] = (
                    margin.hourly_borrow_rate if margin else HOURLY_BORROW_FALLBACK
                )
                self.collateral_ratios[symbol] = (
                    margin.collateral_ratio if margin else Decimal("0.98")
                )
        self.quotes_stale = failures == len(self.cfg.trading.assets)

    # --- ledger ---------------------------------------------------------------------

    def ledger(self, session: Session, now: datetime, track: str = "primary") -> PaperAccount:
        track_id = 1 if track == "primary" else 2
        acct = session.get(PaperAccount, track_id)
        capital = self.cfg.trading.capital_max_usdt
        if acct is None:
            acct = PaperAccount(
                id=track_id,
                track=track,
                starting_capital=capital,
                cash=capital,
                equity=capital,
                day_start_equity=capital,
                day_date=now.replace(hour=0, minute=0, second=0, microsecond=0),
                day_high_equity=capital,
                updated_at=now,
            )
            session.add(acct)
            session.commit()
        elif acct.starting_capital == 0 and capital > 0:
            # The owner set capital after the ledger was created: adopt it once.
            acct.starting_capital = acct.cash = acct.equity = capital
            acct.day_start_equity = acct.day_high_equity = capital
            session.commit()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if acct.day_date < day:
            acct.day_date, acct.day_start_equity, acct.day_high_equity = (
                day,
                acct.equity,
                acct.equity,
            )
        return acct

    # --- control --------------------------------------------------------------------

    def apply_controls(self, session: Session, now: datetime) -> bool:
        """Returns True when a kill is requested this pass."""
        kill = False
        pending = session.scalars(
            select(ControlRequest)
            .where(ControlRequest.applied_at.is_(None))
            .order_by(ControlRequest.id)
        ).all()
        state = session.get(RiskState, 1)
        for req in pending:
            if req.kind == "kill":
                kill, self.frozen = True, True
                req.result = "closing all positions, frozen"
            elif req.kind == "pause":
                self.frozen = True
                req.result = "frozen: no new positions"
            elif req.kind == "resume":
                self.frozen = False
                if state is not None:
                    state.emergency_brake, state.brake_reason = False, None
                    state.paused_until = None
                req.result = "resumed"
            req.applied_at = now
        session.commit()
        return kill

    # --- positions ----------------------------------------------------------------

    async def manage_open(
        self, session: Session, now: datetime, kill: bool, close_all: bool
    ) -> None:
        if self.quotes_stale:
            log.warning("quotes stale: stops stay armed, no exits evaluated this tick")
            return
        rows = session.scalars(
            select(Position).where(Position.status.in_(["open", "closing"]))
        ).all()
        for row in rows:
            quote = self.quotes.get(row.symbol)
            if quote is None:
                continue
            pos = self._to_sim(row)
            pos = sim.accrue_interest(
                pos, now, self.borrow_rates.get(row.symbol, HOURLY_BORROW_FALLBACK), quote.mid
            )
            pos = sim.update_trail(pos, quote)
            reason = sim.exit_reason(pos, quote, now, kill=kill)
            if reason is None and close_all:
                reason = ExitReason.RISK
            if reason is None and row.status == "closing":
                reason = ExitReason(row.close_reason or "risk")  # retry a pending exit
            row.interest, row.trail_stop, row.updated_at = pos.interest, pos.trail_stop, now
            row.liquidation_price = pos.liquidation_price
            if reason is None:
                continue
            result = await self.gateway.close(
                row.symbol, Side(row.direction), row.qty, f"{row.order_link_id}-x", quote
            )
            if result.outcome is Outcome.UNREACHABLE:
                row.status, row.close_reason = "closing", reason.value
                log.error("%s: exit (%s) could not reach the venue; retrying", row.asset, reason)
                continue
            if result.outcome is Outcome.PARTIAL and result.filled_qty < row.qty:
                # Book the filled part, keep the remainder open with the same stops.
                remainder = row.qty - result.filled_qty
                pos = sim.PaperPosition(**{**pos.__dict__, "qty": result.filled_qty})
                row.qty, row.margin = (
                    remainder,
                    row.margin * remainder / (remainder + result.filled_qty),
                )
                row.notional = remainder * (row.entry_price or quote.mid)
                row.status, row.close_reason = "open", None
                exit_quote = Quote(
                    result.avg_price or quote.bid, result.avg_price or quote.ask, now
                )
                fill = sim.close_position(pos, exit_quote, Decimal(0), reason)
                fill = sim.Fill(
                    fill.price,
                    result.fee,
                    fill.gross_pnl,
                    fill.net_pnl - result.fee,
                    fill.pnl_price_pct,
                    fill.pnl_margin_pct,
                )
                acct = self.ledger(session, now, row.track)
                acct.cash += pos.margin + fill.net_pnl
                acct.realized_pnl += fill.net_pnl
                log.warning(
                    "%s: partial exit %s of %s",
                    row.asset,
                    result.filled_qty,
                    row.qty + result.filled_qty,
                )
                continue
            exit_quote = (
                Quote(result.avg_price, result.avg_price, now) if result.avg_price else quote
            )
            fill = sim.close_position(pos, exit_quote, Decimal(0), reason)
            fill = sim.Fill(
                fill.price,
                result.fee,
                fill.gross_pnl,
                fill.net_pnl - result.fee,
                fill.pnl_price_pct,
                (fill.net_pnl - result.fee) / pos.margin if pos.margin else Decimal(0),
            )
            row.status, row.closed_at, row.close_reason = "closed", now, reason.value
            row.exit_price, row.fees = fill.price, row.fees + fill.fee
            if row.track == "primary":
                await self.post(session, row, "close", None, now)
            row.pnl, row.pnl_price_pct, row.pnl_margin_pct = (
                fill.net_pnl,
                fill.pnl_price_pct,
                fill.pnl_margin_pct,
            )
            acct = self.ledger(session, now, row.track)
            acct.cash += row.margin + fill.net_pnl
            acct.realized_pnl += fill.net_pnl
            acct.fees_paid += fill.fee
            acct.interest_paid += row.interest
            if reason is ExitReason.LIQUIDATION:
                acct.liquidations += 1
            log.info(
                "closed %s %s %s at %s: %.2f (%s)",
                row.mode,
                row.asset,
                row.direction,
                fill.price,
                fill.net_pnl,
                reason.value,
            )
        session.commit()

    async def open_new(self, session: Session, now: datetime) -> None:
        if self.frozen or self.quotes_stale:
            return
        tracks = ["primary", "max"] if self.mode == "paper" else ["primary"]
        for track in tracks:
            await self._open_track(session, now, track)

    async def _open_track(self, session: Session, now: datetime, track: str) -> None:
        fee = TAKER_FEE.get(self.cfg.exchange.quote, Decimal("0.001"))
        acct = self.ledger(session, now, track)
        plan_key = "plan" if track == "primary" else "plan_max"
        open_assets = set(
            session.scalars(
                select(Position.asset).where(Position.status == "open", Position.track == track)
            ).all()
        )
        recent = now - timedelta(hours=self.cfg.trading.cycle_hours)
        rows = session.scalars(
            select(DecisionRecord)
            .where(DecisionRecord.action == "open", DecisionRecord.ts >= recent)
            .order_by(DecisionRecord.ts.desc())
        ).all()
        seen: set[str] = set()
        for d in rows:
            if d.asset in seen or d.asset in open_assets:
                continue
            seen.add(d.asset)
            if session.scalar(
                select(func.count())
                .select_from(Position)
                .where(Position.decision_id == d.id, Position.track == track)
            ):
                continue  # already executed
            plan = (d.proposal or {}).get(plan_key)
            symbol = self.cfg.symbol(d.asset)
            quote = self.quotes.get(symbol)
            if not plan or quote is None:
                continue
            side = Side.LONG if d.direction == "long" else Side.SHORT
            notional, leverage = Decimal(plan["notional"]), Decimal(plan["leverage"])
            entry_mid = Decimal(plan["entry"])
            # Limit near mid (SPEC §9): take it only while the market is within ±0.5% of entry.
            if abs(quote.mid / entry_mid - 1) > Decimal("0.005"):
                continue
            if notional / leverage > acct.cash:
                log.warning(
                    "%s: margin %s exceeds cash %s", d.asset, notional / leverage, acct.cash
                )
                continue
            try:
                pos = sim.open_position(
                    side,
                    notional,
                    leverage,
                    quote,
                    Decimal(plan["stop"]),
                    Decimal(plan["target"]),
                    int(plan["max_hold_hours"]),
                    fee,
                    self.qty_steps.get(symbol, QTY_STEP_FALLBACK),
                    collateral_ratio=self.collateral_ratios.get(symbol, Decimal("0.98")),
                    maintenance_rate=self.cfg.risk.maintenance_margin_rate,
                )
            except ValueError as exc:
                log.warning("%s: %s", d.asset, exc)
                continue
            link = f"{'paper' if self.mode == 'paper' else 'live'}-{uuid.uuid4().hex[:20]}"
            result = await self.gateway.open(
                symbol, side, pos.qty, bool(plan.get("borrow")), link, quote
            )
            if result.outcome is Outcome.UNREACHABLE:
                log.error("%s: venue unreachable; entry retried next tick", d.asset)
                continue
            if result.outcome in (Outcome.REJECTED, Outcome.BORROW_FAILED):
                d.action = result.outcome.value
                log.error("%s: entry %s: %s", d.asset, result.outcome.value, result.detail)
                continue
            if result.outcome is Outcome.PARTIAL:
                if result.filled_qty <= 0:
                    d.action = "rejected"
                    continue
                scale = result.filled_qty / pos.qty
                pos = sim.PaperPosition(
                    **{
                        **pos.__dict__,
                        "qty": result.filled_qty,
                        "margin": pos.margin * scale,
                        "borrowed": pos.borrowed * scale,
                        "fees": result.fee,
                    }
                )
                log.warning("%s: partial entry %s of %s", d.asset, result.filled_qty, notional)
            elif result.avg_price and result.avg_price != pos.entry:
                pos = sim.PaperPosition(
                    **{**pos.__dict__, "entry": result.avg_price, "fees": result.fee}
                )
            row = Position(
                decision_id=d.id,
                cycle_id=d.cycle_id,
                mode=self.mode,
                track=track,
                asset=d.asset,
                symbol=symbol,
                direction=side.value,
                status="open",
                qty=pos.qty,
                entry_price=pos.entry,
                leverage=pos.leverage,
                margin=pos.margin,
                notional=pos.qty * pos.entry,
                borrowed=pos.borrowed,
                stop=pos.stop,
                target=pos.target,
                max_hold_hours=pos.max_hold_hours,
                opened_at=now,
                fees=pos.fees,
                liquidation_price=pos.liquidation_price,
                order_link_id=link,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            if track == "primary":
                await self.post(session, row, "open", self._reason_for(d), now)
            acct.cash -= pos.margin + pos.fees
            acct.fees_paid += pos.fees
            open_assets.add(d.asset)
            log.info(
                "opened %s %s %s qty %s at %s, %sx, stop %s target %s",
                self.mode,
                d.asset,
                side.value,
                pos.qty,
                pos.entry,
                leverage,
                pos.stop,
                pos.target,
            )
        session.commit()

    def posting_live(self) -> bool:
        p = self.cfg.posting
        return p.enabled and (self.mode == "live" or p.post_in_shadow)

    @staticmethod
    def _reason_for(d: DecisionRecord) -> str | None:
        reasons = (d.proposal or {}).get("reasons") or []
        return str(reasons[0])[:120] if reasons else None

    async def post(
        self, session: Session, row: Position, kind: str, reason: str | None, now: datetime
    ) -> None:
        try:
            await poster.enqueue(
                session,
                self.cfg,
                row,
                kind,
                reason=reason,
                style=self.style,
                dry_run=not self.posting_live(),
                now=now,
            )
        except Exception:  # posting must never block trading
            log.exception("could not queue %s post for %s", kind, row.asset)

    def _to_sim(self, row: Position) -> sim.PaperPosition:
        assert row.entry_price is not None and row.opened_at is not None
        return sim.PaperPosition(
            side=Side(row.direction),
            qty=row.qty,
            entry=row.entry_price,
            leverage=row.leverage,
            margin=row.margin,
            borrowed=row.borrowed,
            stop=row.stop,
            target=row.target,
            opened_at=row.opened_at,
            max_hold_hours=row.max_hold_hours,
            fees=row.fees,
            interest=row.interest,
            trail_stop=row.trail_stop,
            last_interest_at=row.updated_at,
            collateral_ratio=self.collateral_ratios.get(row.symbol, Decimal("0.98")),
            maintenance_rate=self.cfg.risk.maintenance_margin_rate,
        )

    # --- accounting ---------------------------------------------------------------

    def snapshot(self, session: Session, now: datetime) -> None:
        summary = []
        for track in ["primary", "max"] if self.mode == "paper" else ["primary"]:
            acct = self.ledger(session, now, track)
            unreal = margin_total = gross = Decimal(0)
            rows = session.scalars(
                select(Position).where(Position.status == "open", Position.track == track)
            ).all()
            for row in rows:
                quote = self.quotes.get(row.symbol)
                if quote is None:
                    continue
                pos = self._to_sim(row)
                unreal += sim.unrealized(pos, quote)
                margin_total += row.margin
                gross += row.qty * quote.mid
            acct.equity = acct.cash + margin_total + unreal
            acct.day_high_equity = max(acct.day_high_equity, acct.equity)
            acct.updated_at = now
            session.merge(
                EquitySnapshot(
                    ts=now,
                    mode=f"{self.mode}:{track}" if self.mode == "paper" else self.mode,
                    equity=acct.equity,
                    cash=acct.cash,
                    open_positions=len(rows),
                    gross_exposure=gross,
                )
            )
            summary.append(f"{track} {len(rows)} open, equity {acct.equity:.2f}")
        session.merge(
            Heartbeat(process="executor", ts=now, detail=f"{self.mode}: " + "; ".join(summary))
        )
        session.commit()

    def risk_close_all(self, session: Session, now: datetime) -> bool:
        """Honour the engine's close-all states: day lock, drawdown pause, emergency brake."""
        state = session.get(RiskState, 1)
        if state is None:
            return False
        if state.emergency_brake:
            self.frozen = True
            return True
        if state.day_locked_until and state.day_locked_until > now:
            return True
        return bool(state.paused_until and state.paused_until > now)

    # --- loop ---------------------------------------------------------------------

    async def tick(self) -> None:
        now = datetime.now(UTC)
        await self.refresh_quotes()
        with new_session() as session:
            kill = self.apply_controls(session, now)
            close_all = self.risk_close_all(session, now)
            await self.manage_open(session, now, kill, close_all)
            if not close_all:
                await self.open_new(session, now)
            self.snapshot(session, now)
            try:
                await poster.flush(session, self.cfg, self.secrets, now)
            except Exception:
                log.exception("poster flush failed")

    async def run(self, once: bool = False) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            if once:
                break
            await asyncio.sleep(TICK_S)
        if self._client is not None:
            await self._client.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(Executor(get_config(), get_secrets()).run(once=args.once))
    return 0


if __name__ == "__main__":
    sys.exit(main())
