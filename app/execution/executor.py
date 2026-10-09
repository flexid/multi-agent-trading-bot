"""Executor process (SPEC §9): the only component that opens or closes positions.

    python -m app.execution.executor            # run forever
    python -m app.execution.executor --once     # one pass, then exit

Every ``TICK_S`` seconds it: refreshes quotes, applies control requests (kill, pause,
resume), closes positions whose stop, target, trailing stop, time-stop or liquidation
hit, opens positions for decisions with ``action = "open"`` that have no position yet,
accrues interest, writes the paper ledger and an equity snapshot, and heartbeats. With
the WebSocket price feed, exits are also checked every ``FAST_S`` between full passes.

Mode is checked here, by this process, from config: ``live`` needs ``live_allowed``
and the go-live checker's verdict (M9); until then every order is paper, whatever the
decision row says. Real orders go through ``BybitGateway`` with an ``orderLinkId``.

Each position row carries its own mode, and that decides the gateway: a paper row never
reaches the exchange, whatever mode the process runs in. In pilot mode the paper tracks
keep running next to the pilot, so the shadow record (and the X storyline) continues.

Exits are never post-only and are retried every pass until the position is flat. Every
real position also has a stop resting on the exchange, ``BACKUP_ATR_MULT`` ATR beyond
the bot's own stop, moved whenever that stop moves and cancelled before the bot exits.
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
from app.execution.gateway import Gateway, OrderOutcome, Outcome, PaperGateway
from app.execution.simulator import ExitReason, Quote, Side
from app.execution.ws_feed import QuoteFeed
from app.social import poster, style

log = logging.getLogger("executor")
TICK_S = 10
ENTRY_CLAMP = Decimal("0.01")  # enter at spot when the plan is more than 1% away
FAST_S = 1  # exit checks between full passes, only on the WebSocket feed
BACKUP_ATR_MULT = Decimal("0.5")  # backup stop sits this many ATR beyond the bot's stop
EXIT_ALERT_AFTER = 3  # passes an exit may stay unfinished before the owner is emailed
# Which tracks run in each process mode, and the mode their positions are stored with.
TRACKS: dict[str, dict[str, str]] = {
    "paper": {"primary": "paper", "max": "paper"},
    "pilot": {"primary": "paper", "max": "paper", "pilot": "pilot"},
    "live": {"primary": "live"},
}
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
        self._client: BybitClient | None = None
        self.mode = self.decide_mode(cfg)  # paper | pilot | live; this process decides
        if gateway is None and self.mode in ("pilot", "live"):
            from app.execution.bybit_gateway import BybitGateway

            self._client = BybitClient(
                secrets.bybit_base_url,
                secrets.bybit_api_key,
                secrets.bybit_api_secret,
                allow_orders=True,
            )
            gateway = BybitGateway(self._client)
            self._collateral_pending = [cfg.base_coin(a) for a in cfg.trading.assets]
        self.paper_gateway = PaperGateway(TAKER_FEE.get(cfg.exchange.quote, Decimal("0.001")))
        self.gateway: Gateway = gateway or self.paper_gateway
        self.feed = feed  # False in tests: quotes are injected
        self.ws: QuoteFeed | None = None
        if feed and cfg.exchange.price_feed == "ws":
            self.ws = QuoteFeed([cfg.symbol(a) for a in cfg.trading.assets])
        self.quotes: dict[str, Quote] = {}
        self.quotes_stale = False
        self.qty_steps: dict[str, Decimal] = {}
        self.borrow_rates: dict[str, Decimal] = {}
        self.collateral_ratios: dict[str, Decimal] = {}
        self.frozen = False  # after a kill switch: no new positions until resumed
        self.style = style.load()
        self._alerted: set[str] = set()  # risk states already emailed this episode

    @staticmethod
    def decide_mode(cfg: Config) -> str:
        """live only when risk_state.mode is live AND live_allowed (go-live checker, M9);
        pilot when the owner enabled it in config; paper otherwise."""
        from app.db.models import RiskState

        try:
            with new_session() as session:
                state = session.get(RiskState, 1)
        except Exception:
            return "paper"
        if state is not None and state.mode == "live" and cfg.trading.live_allowed:
            return "live"
        if cfg.pilot.enabled:
            return "pilot"
        return "paper"

    def gateway_for(self, row_mode: str) -> Gateway:
        """Paper rows fill on the paper book even while the process trades for real."""
        if row_mode == "paper" and self.mode != "paper":
            return self.paper_gateway
        return self.gateway

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
        """Quotes from the WebSocket feed, or the REST ticker when the stream has gone
        quiet (or is not configured). On failure keep the last ones and mark them stale.
        Stops stay armed on the last known quote and fire on the next fresh one."""
        if not self.feed:
            return
        client = await self.client()
        now = datetime.now(UTC)
        failures = 0
        for asset in self.cfg.trading.assets:
            symbol = self.cfg.symbol(asset)
            pushed = self.ws.fresh(symbol, now) if self.ws is not None else None
            if pushed is not None:
                self.quotes[symbol] = pushed
            else:
                try:
                    t = await client.ticker(symbol)
                except Exception as exc:
                    failures += 1
                    log.warning("ticker %s failed: %s", symbol, exc)
                    continue
                self.quotes[symbol] = Quote(t.bid1_price, t.ask1_price, now)
            if symbol not in self.qty_steps:
                try:
                    await self._load_terms(client, asset, symbol)
                except Exception as exc:
                    log.warning("instrument terms %s failed: %s", symbol, exc)
        self.quotes_stale = failures == len(self.cfg.trading.assets)

    def refresh_pushed(self) -> bool:
        """Fast pass: take what the stream has. False when no quote is fresh."""
        if self.ws is None:
            return False
        now = datetime.now(UTC)
        fresh = 0
        for asset in self.cfg.trading.assets:
            symbol = self.cfg.symbol(asset)
            pushed = self.ws.fresh(symbol, now)
            if pushed is not None:
                self.quotes[symbol] = pushed
                fresh += 1
        return fresh > 0

    async def _load_terms(self, client: BybitClient, asset: str, symbol: str) -> None:
        inst = await client.instrument(symbol)
        margin = await client.margin_coin(inst.base_coin if inst else asset)
        self.borrow_rates[symbol] = margin.hourly_borrow_rate if margin else HOURLY_BORROW_FALLBACK
        self.collateral_ratios[symbol] = margin.collateral_ratio if margin else Decimal("0.98")
        if inst is not None:
            self.qty_steps[symbol] = inst.lot_size_filter.base_precision

    # --- ledger ---------------------------------------------------------------------

    def ledger(self, session: Session, now: datetime, track: str = "primary") -> PaperAccount:
        track_id = {"primary": 1, "max": 2, "pilot": 3}[track]
        acct = session.get(PaperAccount, track_id)
        capital = (
            self.cfg.pilot.capital_usdt if track == "pilot" else self.cfg.trading.capital_max_usdt
        )
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
                self._alert(
                    "dorkbot: kill switch applied",
                    f"Requested by {req.source}. Reason: {req.reason or '-'}. "
                    "All positions are being closed; the executor is frozen until resume.",
                )
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

    def apply_param_changes(self, session: Session, now: datetime) -> None:
        """Parameter changes requested from the admin, re-checked against the hard bounds
        in code before they touch config.toml. Leverage never above 10 (SPEC §12 M8b)."""
        from app.admin.app import EDITABLE
        from app.db.models import ParamChange

        pending = session.scalars(
            select(ParamChange).where(ParamChange.applied_at.is_(None)).order_by(ParamChange.id)
        ).all()
        if not pending:
            return
        import tomllib

        from app.config import CONFIG_PATH, get_config

        text = CONFIG_PATH.read_text()
        for req in pending:
            req.applied_at = now
            bounds = EDITABLE.get(req.key)
            try:
                value = Decimal(req.new_value)
            except Exception:
                req.result = "rejected: not a number"
                continue
            if bounds is None or not (bounds[0] <= value <= bounds[1]):
                req.result = f"rejected: outside bounds {bounds}"
                continue
            section, name = req.key.split(".", 1)
            new_text, n = _set_toml_value(text, section, name, value)
            if n != 1:
                req.result = "rejected: key not found in config.toml"
                continue
            try:
                tomllib.loads(new_text)
            except tomllib.TOMLDecodeError:
                req.result = "rejected: would break config.toml"
                continue
            text = new_text
            req.result = "applied"
        CONFIG_PATH.write_text(text)
        get_config.cache_clear()
        self.cfg = get_config()
        session.commit()
        log.info("applied %d parameter change(s)", sum(1 for r in pending if r.result == "applied"))

    # --- positions ----------------------------------------------------------------

    async def manage_open(
        self, session: Session, now: datetime, kill: bool, close_all: bool, *, fast: bool = False
    ) -> None:
        """Evaluate and work every exit. ``fast``: the in-between pass on pushed quotes;
        it skips the backup-stop housekeeping, which the full pass does."""
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
            gw = self.gateway_for(row.mode)
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
                if not fast and row.mode != "paper":
                    # The exchange may have triggered the backup while we were away.
                    if await self._backup_triggered(row, gw, quote, now):
                        reason = ExitReason.STOP
                    else:
                        await self._sync_backup_stop(row, gw)
                if reason is None:
                    continue
            if row.status != "closing":
                row.status, row.close_reason = "closing", reason.value
            if not await self._clear_backup_stop(row, gw, quote):
                # The stop still rests and protects; sending our own exit next to it
                # could sell twice. Try again next pass.
                self._exit_pending(row, reason, "backup stop could not be cancelled")
                continue
            remaining = row.qty - row.exit_filled_qty
            if remaining > 0:
                result = await gw.close(
                    row.symbol, Side(row.direction), remaining, f"{row.order_link_id}-x", quote
                )
                self._add_exit_fill(row, result, quote)
                if result.outcome is not Outcome.FILLED and row.exit_filled_qty < row.qty:
                    self._exit_pending(row, reason, f"{result.outcome.value} {result.detail}")
                    continue
            await self._book_exit(
                session, row, pos, quote, ExitReason(row.close_reason or reason.value), now
            )
        session.commit()

    def _exit_pending(self, row: Position, reason: ExitReason, detail: str) -> None:
        """An exit that is not flat yet stays ``closing`` and is retried every pass."""
        row.exit_attempts += 1
        log.error(
            "%s %s: exit (%s) not flat after %d pass(es), %s of %s filled: %s",
            row.mode,
            row.asset,
            reason.value,
            row.exit_attempts,
            row.exit_filled_qty,
            row.qty,
            detail.strip(),
        )
        key = f"exit:{row.id}"
        if (
            row.mode != "paper"
            and row.exit_attempts >= EXIT_ALERT_AFTER
            and key not in self._alerted
        ):
            self._alerted.add(key)
            self._alert(
                f"dorkbot: {row.asset} exit not filled",
                f"The {reason.value} exit on {row.asset} ({row.mode}) is still open after "
                f"{row.exit_attempts} passes: {row.exit_filled_qty} of {row.qty} filled. "
                f"Last answer: {detail.strip()}. The executor keeps retrying every pass.",
            )

    @staticmethod
    def _add_exit_fill(row: Position, result: OrderOutcome, quote: Quote) -> None:
        if result.filled_qty <= 0:
            return
        price = result.avg_price or (quote.bid if row.direction == "long" else quote.ask)
        row.exit_filled_qty += result.filled_qty
        row.exit_value += result.filled_qty * price
        row.exit_fee += result.fee

    async def _book_exit(
        self,
        session: Session,
        row: Position,
        pos: sim.PaperPosition,
        quote: Quote,
        reason: ExitReason,
        now: datetime,
    ) -> None:
        """The position is flat: book it once, at the average of everything that filled."""
        if row.exit_filled_qty > 0:
            price = row.exit_value / row.exit_filled_qty
            exit_quote = Quote(price, price, now)
        else:
            exit_quote = quote
        # A real position that our liquidation guard closed is booked at its real fill,
        # not at the modelled liquidation price with the whole margin gone.
        calc = (
            ExitReason.STOP if reason is ExitReason.LIQUIDATION and row.mode != "paper" else reason
        )
        fill = sim.close_position(pos, exit_quote, Decimal(0), calc)
        net = fill.net_pnl - row.exit_fee
        row.status, row.closed_at, row.close_reason = "closed", now, reason.value
        row.exit_price, row.fees = fill.price, row.fees + row.exit_fee
        if row.track == "primary":
            await self.post(session, row, "close", None, now)
        row.pnl, row.pnl_price_pct, row.pnl_margin_pct = (
            net,
            fill.pnl_price_pct,
            net / pos.margin if pos.margin else Decimal(0),
        )
        acct = self.ledger(session, now, row.track)
        acct.cash += row.margin + net
        acct.realized_pnl += net
        acct.fees_paid += row.exit_fee
        acct.interest_paid += row.interest
        if reason is ExitReason.LIQUIDATION and row.mode == "paper":
            acct.liquidations += 1
        self._alerted.discard(f"exit:{row.id}")
        log.info(
            "closed %s %s %s at %s: %.2f (%s)",
            row.mode,
            row.asset,
            row.direction,
            fill.price,
            net,
            reason.value,
        )

    # --- exchange-side backup stop ---------------------------------------------------

    @staticmethod
    def backup_trigger(row: Position) -> Decimal:
        """``BACKUP_ATR_MULT`` ATR beyond the stop the bot itself enforces (the trailing
        stop once armed). Without an ATR on the row, the stop distance stands in for it."""
        assert row.entry_price is not None
        atr = row.atr if row.atr and row.atr > 0 else abs(row.entry_price - row.stop)
        offset = BACKUP_ATR_MULT * atr
        if row.direction == "long":
            stop = max(row.stop, row.trail_stop) if row.trail_stop is not None else row.stop
            return stop - offset
        stop = min(row.stop, row.trail_stop) if row.trail_stop is not None else row.stop
        return stop + offset

    async def _sync_backup_stop(self, row: Position, gw: Gateway) -> None:
        """Place the backup stop, or move it when the bot's stop moved. Cancel first, then
        place: two resting stops could both trigger. If the new one does not land the
        position is without a backup until the next pass, with the bot's own stop live."""
        if not (gw.supports_backup_stop and self.cfg.exchange.backup_stop):
            return
        if row.status != "open":
            return
        want = self.backup_trigger(row)
        if row.backup_stop_link is not None and row.backup_stop_price == want:
            return
        if row.backup_stop_link is not None:
            if not await gw.cancel_backup_stop(row.symbol, row.backup_stop_link):
                return  # the old one still rests; try again next pass
            row.backup_stop_link = row.backup_stop_price = None
        link = f"bk-{uuid.uuid4().hex[:24]}"
        qty = row.qty - row.exit_filled_qty
        if await gw.place_backup_stop(row.symbol, Side(row.direction), qty, want, link):
            row.backup_stop_link, row.backup_stop_price = link, want
            self._alerted.discard(f"backup:{row.id}")
            return
        key = f"backup:{row.id}"
        if key not in self._alerted:
            self._alerted.add(key)
            self._alert(
                f"dorkbot: no backup stop on {row.asset}",
                f"The exchange-side backup stop for {row.asset} ({row.mode}) could not be "
                "placed. The bot's own stop is active; the executor retries every pass.",
            )

    async def _backup_triggered(
        self, row: Position, gw: Gateway, quote: Quote, now: datetime
    ) -> bool:
        """True when the exchange filled the backup stop; the fill is taken onto the row."""
        if row.backup_stop_link is None:
            return False
        fill = await gw.backup_stop_fill(row.symbol, row.backup_stop_link)
        if fill is None or fill.outcome is Outcome.UNREACHABLE:
            return False
        self._add_exit_fill(row, fill, quote)
        row.backup_stop_link = row.backup_stop_price = None
        log.error("%s %s: the exchange-side backup stop triggered", row.mode, row.asset)
        self._alert(
            f"dorkbot: backup stop triggered on {row.asset}",
            f"The exchange-side stop on {row.asset} ({row.mode}) filled {fill.filled_qty} at "
            f"{fill.avg_price}. The bot's own stop did not act first; check the executor.",
        )
        return True

    async def _clear_backup_stop(self, row: Position, gw: Gateway, quote: Quote) -> bool:
        """Before the bot exits: cancel the backup, then read whether it had filled.
        False means its state is unknown and no exit order may be sent this pass."""
        link = row.backup_stop_link
        if link is None:
            return True
        if not await gw.cancel_backup_stop(row.symbol, link):
            return False
        fill = await gw.backup_stop_fill(row.symbol, link)
        if fill is not None and fill.outcome is Outcome.UNREACHABLE:
            return False
        if fill is not None:
            self._add_exit_fill(row, fill, quote)
        row.backup_stop_link = row.backup_stop_price = None
        return True

    async def open_new(self, session: Session, now: datetime) -> None:
        if self.frozen or self.quotes_stale:
            return
        for track, track_mode in TRACKS[self.mode].items():
            await self._open_track(session, now, track, track_mode)

    async def _open_track(
        self, session: Session, now: datetime, track: str, track_mode: str
    ) -> None:
        gw = self.gateway_for(track_mode)
        fee = TAKER_FEE.get(self.cfg.exchange.quote, Decimal("0.001"))
        acct = self.ledger(session, now, track)
        plan_key = "plan_max" if track == "max" else "plan"
        open_assets = set(
            session.scalars(
                select(Position.asset).where(
                    Position.status.in_(["open", "closing"]), Position.track == track
                )
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
            if track == "pilot":  # owner: 200 USDT at 1x, to test fills, slippage, borrowing
                leverage = Decimal(1)
                notional = min(
                    notional,
                    acct.cash,
                    self.cfg.pilot.capital_usdt * self.cfg.trading.capital_share_per_asset,
                )
            entry_mid = Decimal(plan["entry"])
            # Owner (2026-10-09): clamp the PMs' entry to spot ±1%. If the plan sits further
            # away, enter here and shift stop and target by the same amount, so the stop
            # distance, the target distance and the risk sizing stay as approved.
            drift = quote.mid / entry_mid - 1
            if abs(drift) > ENTRY_CLAMP:
                offset = quote.mid - entry_mid
                plan = {
                    **plan,
                    "entry": str(quote.mid),
                    "stop": str(Decimal(plan["stop"]) + offset),
                    "target": str(Decimal(plan["target"]) + offset),
                }
                log.info("%s: entry clamped to spot (%+.2f%% from plan)", d.asset, drift * 100)
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
            link = f"{'paper' if track_mode == 'paper' else 'live'}-{uuid.uuid4().hex[:20]}"
            result = await gw.open(symbol, side, pos.qty, bool(plan.get("borrow")), link, quote)
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
                mode=track_mode,
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
                atr=Decimal(plan["atr"]) if plan.get("atr") else None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            if track_mode != "paper":
                await self._sync_backup_stop(row, gw)
            if track == "primary":
                await self.post(session, row, "open", self._reason_for(d), now)
            acct.cash -= pos.margin + pos.fees
            acct.fees_paid += pos.fees
            open_assets.add(d.asset)
            log.info(
                "opened %s %s %s qty %s at %s, %sx, stop %s target %s",
                track_mode,
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
        """First PM reason that passes the post whitelist; indicator readings never pass."""
        from app.social.whitelist import check

        for r in (d.proposal or {}).get("reasons") or []:
            text = str(r)[:120].rstrip(".")
            if check(text).ok:
                return text
        return None

    async def post(
        self, session: Session, row: Position, kind: str, reason: str | None, now: datetime
    ) -> None:
        if row.mode == "pilot" or row.track != "primary":
            return  # the pilot is an execution test, not part of the X storyline
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
        for track, track_mode in TRACKS[self.mode].items():
            acct = self.ledger(session, now, track)
            unreal = margin_total = gross = Decimal(0)
            rows = session.scalars(
                select(Position).where(
                    Position.status.in_(["open", "closing"]), Position.track == track
                )
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
                    mode=f"{track_mode}:{track}" if track_mode != "live" else track_mode,
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

    def _alert(self, subject: str, body: str) -> None:
        """Owner email via the admin's notify module; never raises into the trading loop."""
        from app.admin import notify

        try:
            notify.send(subject, body)
        except Exception as exc:
            log.warning("alert failed: %s", exc)

    def risk_close_all(self, session: Session, now: datetime) -> bool:
        """Honour the engine's close-all states: day lock, drawdown pause, emergency brake."""
        state = session.get(RiskState, 1)
        if state is None:
            return False
        alerted = self._alerted
        for flag, cond, subject in (
            ("brake", state.emergency_brake, "dorkbot: EMERGENCY BRAKE, waiting for you"),
            (
                "pause",
                bool(state.paused_until and state.paused_until > now),
                "dorkbot: drawdown pause, closing everything for 72 h",
            ),
            (
                "daylock",
                bool(state.day_locked_until and state.day_locked_until > now),
                "dorkbot: day loss stop, closed everything until 00:00 UTC",
            ),
        ):
            if cond and flag not in alerted:
                alerted.add(flag)
                self._alert(
                    subject,
                    f"{subject}. Reason: {state.brake_reason or 'risk rule'}. Mode {self.mode}. "
                    "Resume from the admin when you are ready.",
                )
            if not cond:
                alerted.discard(flag)
        if state.emergency_brake:
            self.frozen = True
            return True
        if state.day_locked_until and state.day_locked_until > now:
            return True
        return bool(state.paused_until and state.paused_until > now)

    # --- loop ---------------------------------------------------------------------

    async def tick(self) -> None:
        now = datetime.now(UTC)
        pending = getattr(self, "_collateral_pending", None)
        if pending and hasattr(self.gateway, "ensure_collateral"):
            done = await self.gateway.ensure_collateral(pending)
            if done:
                log.info("collateral on for %s", ", ".join(done))
                self._collateral_pending = []
        await self.refresh_quotes()
        with new_session() as session:
            self.apply_param_changes(session, now)
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

    async def fast_tick(self) -> None:
        """Between full passes, on pushed quotes only: stops, targets, trailing stops,
        time-stops and pending exits. Controls, entries and accounting wait for the
        full pass."""
        if self.quotes_stale or not self.refresh_pushed():
            return
        now = datetime.now(UTC)
        with new_session() as session:
            await self.manage_open(session, now, False, False, fast=True)

    async def run(self, once: bool = False) -> None:
        if self.ws is not None and not once:
            self.ws.start()
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            if once:
                break
            if self.ws is None:
                await asyncio.sleep(TICK_S)
                continue
            for _ in range(TICK_S // FAST_S - 1):
                await asyncio.sleep(FAST_S)
                try:
                    await self.fast_tick()
                except Exception:
                    log.exception("fast tick failed")
            await asyncio.sleep(FAST_S)
        if self.ws is not None:
            await self.ws.stop()
        if self._client is not None:
            await self._client.aclose()


def _set_toml_value(text: str, section: str, name: str, value: Decimal) -> tuple[str, int]:
    """Replace `name = ...` inside `[section]` keeping comments; returns (text, replacements)."""
    import re

    lines = text.splitlines(keepends=True)
    current, count = None, 0
    for i, line in enumerate(lines):
        m = re.match(r"^\[([^\]]+)\]", line)
        if m:
            current = m.group(1)
            continue
        if current == section and re.match(rf"^{re.escape(name)}\s*=", line):
            comment = line.split("#", 1)[1].rstrip("\n") if "#" in line else ""
            lines[i] = f"{name} = {value:g}" + (f"  #{comment}" if comment else "") + "\n"
            count += 1
    return "".join(lines), count


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
