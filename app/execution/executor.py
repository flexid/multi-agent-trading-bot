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
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config, Secrets, get_config, get_secrets
from app.db.models import (
    Candle,
    ControlRequest,
    DecisionRecord,
    EquitySnapshot,
    Heartbeat,
    PaperAccount,
    Position,
    RiskState,
)
from app.db.session import new_session
from app.decision import goal_cycle
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
# The ledgers whose banked day goal starts a new decision cycle: the record the site shows.
STORY_TRACKS = ("primary", "live")

TRACKS: dict[str, dict[str, str]] = {
    # "inverse" (owner, for fun, 2026-10-09): the opposite of every primary trade, on paper,
    # with the stop and target mirrored around the entry. Tells whether the bot's reasoning
    # has an edge, no edge, or the reverse of one.
    # v11: one paper book. The max (leveraged) and inverse (mirror) books answered their
    # questions; their open positions run out under their stops, nothing new opens there.
    "paper": {"primary": "paper"},
    "pilot": {"primary": "paper", "pilot": "pilot"},
    # One real-money track; the paper tracks keep running beside it as the control group
    # (same decisions at the live rules and at risk_per_trade_max), which is the evidence
    # for stepping live risk up toward the owner's goal.
    "live": {"primary": "paper", "live": "live"},
}
LEDGER_IDS = {"primary": 1, "max": 2, "pilot": 3, "live": 4, "inverse": 5}
TAKER_FEE = {"USDT": Decimal("0.001"), "USDC": Decimal("0.0005")}
LIQ_WARN_ROOM = Decimal("0.30")  # mail when under 30% of the entry-to-liquidation distance is left
CASH_FIT_BUFFER = Decimal("0.995")  # leave the entry fee in cash when sizing to what is free
MIN_FIT_FRACTION = Decimal("0.25")  # a trade smaller than a quarter of the plan is skipped
QTY_STEP_FALLBACK = Decimal("0.000001")
HOURLY_BORROW_FALLBACK = Decimal("0.000005")


def tpl_holding(row: Position, now: datetime) -> str:
    hours = (now - row.opened_at).total_seconds() / 3600 if row.opened_at else 0.0
    return f"{hours:.0f}h"


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
            self.ws = QuoteFeed([symbol for _, symbol in self._traded_symbols()])
        self.quotes: dict[str, Quote] = {}
        self.quotes_stale = False
        self.qty_steps: dict[str, Decimal] = {}
        self.borrow_rates: dict[str, Decimal] = {}
        self.collateral_ratios: dict[str, Decimal] = {}
        self.frozen = False  # after a kill switch: no new positions until resumed
        self.kill_real_only = False  # a self-test kill leaves the paper tracks alone
        self._candle_ref: dict[str, tuple[int, Decimal]] = {}  # 15-minute bucket, last mid
        self._orphan_checked_at = datetime.min.replace(tzinfo=UTC)
        self._orphan_seen: set[str] = set()  # unowned backup stops seen once already
        # sleeves under their day-loss lock (directions, since when), per pass
        self._locked_cache: dict[str, dict[str, Any]] = {}
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

    def _traded_symbols(self) -> list[tuple[str, str]]:
        """The configured assets plus any asset still open from an earlier universe
        (BNB, SPX6900 after 2026-10-10): those positions are managed to their close."""
        pairs = [(a, self.cfg.symbol(a)) for a in self.cfg.trading.assets]
        known = {s for _, s in pairs}
        try:
            with new_session() as session:
                rows = session.execute(
                    select(Position.asset, Position.symbol)
                    .where(Position.status.in_(["open", "closing"]))
                    .distinct()
                ).all()
        except Exception:
            return pairs
        for asset, symbol in rows:
            if symbol not in known:
                pairs.append((asset, symbol))
                known.add(symbol)
        return pairs

    async def refresh_quotes(self) -> None:
        """Quotes from the WebSocket feed, or the REST ticker when the stream has gone
        quiet (or is not configured). On failure keep the last ones and mark them stale.
        Stops stay armed on the last known quote and fire on the next fresh one."""
        if not self.feed:
            return
        client = await self.client()
        now = datetime.now(UTC)
        failures = 0
        for asset, symbol in self._traded_symbols():
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
        self.quotes_stale = failures >= len(self.cfg.trading.assets)

    def refresh_pushed(self) -> bool:
        """Fast pass: take what the stream has. False when no quote is fresh."""
        if self.ws is None:
            return False
        now = datetime.now(UTC)
        fresh = 0
        for _asset, symbol in self._traded_symbols():
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

    @staticmethod
    def _exchange_equity(session: Session) -> Decimal | None:
        from app.db.models import AccountSnapshot

        snap = session.execute(
            select(AccountSnapshot).order_by(AccountSnapshot.ts.desc()).limit(1)
        ).scalar_one_or_none()
        if snap is None or not snap.total_equity or snap.total_equity <= 0:
            return None
        return Decimal(str(snap.total_equity))

    def ledger(self, session: Session, now: datetime, track: str = "primary") -> PaperAccount:
        track_id = LEDGER_IDS[track]
        acct = session.get(PaperAccount, track_id)
        if track == "pilot":
            capital = self.cfg.pilot.capital_usdt
        elif track == "live":
            # The live record starts at what the subaccount really holds on go-live day;
            # the risk engine's "% against starting capital" reads the same number.
            capital = self._exchange_equity(session) or self.cfg.trading.capital_max_usdt
        else:
            capital = self.cfg.trading.capital_max_usdt
        if acct is None and track == "live":
            state = session.get(RiskState, 1)
            if state is not None and not state.starting_capital:
                state.starting_capital = capital
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
            acct.goal_base = None  # a new day: the goal counts from its start again
        return acct

    # --- control --------------------------------------------------------------------

    def apply_controls(self, session: Session, now: datetime) -> bool:
        """Returns True when a kill is requested this pass."""
        kill = False
        pending = session.scalars(
            select(ControlRequest)
            # "cycle" is the scheduler's (app/decision/goal_cycle.py), "arena_round" the arena's
            .where(
                ControlRequest.applied_at.is_(None),
                ControlRequest.kind.not_in(["cycle", "arena_round"]),
            )
            .order_by(ControlRequest.id)
        ).all()
        state = session.get(RiskState, 1)
        for req in pending:
            if req.kind == "kill":
                kill, self.frozen = True, True
                # The live self-test exercises the kill path on the exchange; the paper
                # tracks are a running experiment and keep their positions.
                self.kill_real_only = req.source == "live_selftest"
                req.result = (
                    "closing real positions, frozen"
                    if self.kill_real_only
                    else "closing all positions, frozen"
                )
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
            elif req.kind == "hold":
                req.result = self._extend_holds(session, req.reason)
            req.applied_at = now
        session.commit()
        return kill

    @staticmethod
    def _extend_holds(session: Session, hours_text: str | None) -> str:
        """The owner's "hold" command: open trades may be held up to this many hours from
        their entry. It only ever lengthens a limit, and never past a week (the bound the
        managers' proposals have)."""
        try:
            hours = int(hours_text or "")
        except ValueError:
            return "rejected: hours must be a whole number"
        if not 1 <= hours <= 168:
            return "rejected: hours must be between 1 and 168"
        rows = session.scalars(
            select(Position).where(Position.status == "open", Position.max_hold_hours < hours)
        ).all()
        for row in rows:
            row.max_hold_hours = hours
        return f"{len(rows)} open position(s) may now be held {hours} hours"

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
        closes = self._closes_15m(now)
        self._locked_cache = self._locked_sleeves(session, now)
        take_profit = self._take_profit(session)
        equity_of: dict[str, Decimal] = {}
        goal_reached = self._day_goal_reached(session, now, rows, take_profit)
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
            if row.entry_price:
                row.mfe_pct, row.mae_pct = sim.excursion(
                    pos.side, row.entry_price, quote, row.mfe_pct, row.mae_pct
                )
            reason = sim.exit_reason(
                pos,
                quote,
                now,
                kill=kill and not (self.kill_real_only and row.mode == "paper"),
                close_15m=closes.get(row.symbol),
            )
            if reason is None and (close_all or self._sleeve_locked(row, now)):
                reason = ExitReason.RISK
            if reason is None and not fast and self._shock_against(session, row, quote, now):
                reason = ExitReason.SHOCK
            if reason is None and row.track in goal_reached:
                reason = ExitReason.TAKE_PROFIT  # the ledger made its day goal: bank it all
            if reason is None and take_profit is not None:
                # Aggressive mode: bank a trade once its net profit reaches its share of
                # the goal, measured on the ledger's equity.
                if row.track not in equity_of:
                    equity_of[row.track] = self.ledger(session, now, row.track).equity
                goal = equity_of[row.track] * take_profit
                # Net of everything: entry fee and interest are in ``unrealized``; the
                # fee for closing is taken off here, so what is banked is at least the goal.
                exit_fee = (
                    quote.mid * pos.qty * TAKER_FEE.get(self.cfg.exchange.quote, Decimal("0.001"))
                )
                net = sim.unrealized(pos, quote) - exit_fee
                if goal > 0 and net >= goal:
                    reason = ExitReason.TAKE_PROFIT
                # ... or once the trade alone is up its share of the margin put in (owner
                # 2026-10-12: "1-3% on a single trade, then close it").
                per_trade = self.cfg.profile.take_profit_trade_pct
                if reason is None and per_trade > 0 and row.margin > 0:
                    ret = net / row.margin
                    peak = row.profit_peak_pct
                    if ret >= per_trade or peak is not None:
                        # Armed at the level; from there the take trails the best return
                        # and fires when the trade gives back a share of it, never below
                        # the level (a fast run to 4% is banked near 4%, not at 2%).
                        peak = max(peak or Decimal(0), ret)
                        row.profit_peak_pct = peak
                        # v14: the floor rides the gain beyond the level (a 4% peak banks
                        # 3% at keep 0.5); below the level nothing is given back
                        keep = self.cfg.profile.take_profit_keep
                        floor = max(per_trade, per_trade + (peak - per_trade) * keep)
                        if ret <= floor and peak > ret:
                            reason = ExitReason.TAKE_PROFIT
            if reason is None and row.status == "closing":
                reason = ExitReason(row.close_reason or "risk")  # retry a pending exit
            row.interest, row.trail_stop, row.updated_at = pos.interest, pos.trail_stop, now
            row.liquidation_price = pos.liquidation_price
            self._warn_near_liquidation(row, pos, quote)
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
        if take_profit is not None:
            self._rebase_flat_ledgers(session, now, take_profit)
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
            ExitReason.STOP_HARD
            if reason is ExitReason.LIQUIDATION and row.mode != "paper"
            else reason
        )
        fill = sim.close_position(pos, exit_quote, Decimal(0), calc)
        net = fill.net_pnl - row.exit_fee
        row.status, row.closed_at, row.close_reason = "closed", now, reason.value
        row.exit_price, row.fees = fill.price, row.fees + row.exit_fee
        row.pnl, row.pnl_price_pct, row.pnl_margin_pct = (
            net,
            fill.pnl_price_pct,
            net / pos.margin if pos.margin else Decimal(0),
        )
        if reason is ExitReason.TIME and row.entry_price and row.mfe_pct is not None:
            # the exit review's evidence (memo 2026-10-26): how far the trade got toward
            # its target before the clock ran out, and how deep it went the other way
            to_target = abs(row.target - row.entry_price) / row.entry_price
            share = row.mfe_pct / to_target if to_target else Decimal(0)
            log.info(
                "%s %s: time exit after %s; best %+.2f%% (%.0f%% of the way to the target), "
                "worst %+.2f%%, closed at %+.2f%% on price",
                row.mode,
                row.asset,
                tpl_holding(row, now),
                row.mfe_pct * 100,
                share * 100,
                (row.mae_pct or 0) * 100,
                (fill.pnl_price_pct or 0) * 100,
            )
        if row.track in ("primary", "live"):
            await self.post(session, row, "close", None, now)  # needs the P&L fields
        acct = self.ledger(session, now, row.track)
        acct.cash += row.margin + net
        acct.realized_pnl += net
        acct.fees_paid += row.exit_fee
        acct.interest_paid += row.interest
        if reason is ExitReason.LIQUIDATION and row.mode == "paper":
            acct.liquidations += 1
        if reason is ExitReason.LIQUIDATION:
            self._alert(
                f"dorkbot: liquidation guard closed {row.asset} ({row.track})",
                f"{row.mode} {row.track} {row.asset} {row.direction} {float(row.leverage):g}x "
                f"was closed by the liquidation guard at {fill.price} (entry {row.entry_price}, "
                f"liquidation level {row.liquidation_price}). P&L {net:+.2f}. "
                + ("Paper money." if row.mode == "paper" else "Real money."),
            )
        self._alerted.discard(f"exit:{row.id}")
        self._alerted.discard(f"liq:{row.id}")
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

    def _day_goal_reached(
        self,
        session: Session,
        now: datetime,
        rows: Sequence[Position],
        take_profit: Decimal | None,
    ) -> set[str]:
        """Aggressive mode's day goal (owner 2026-10-12): the ledgers whose equity, with
        every open trade valued now and its closing fee taken off, stands at least the
        goal above the base. Everything on such a ledger is closed. The base is the day's
        start and moves up to the banked equity each time, so the bot keeps going for the
        next step instead of stopping for the day."""
        if take_profit is None:
            return set()
        fee_rate = TAKER_FEE.get(self.cfg.exchange.quote, Decimal("0.001"))
        by_track: dict[str, list[Position]] = {}
        for row in rows:
            by_track.setdefault(row.track, []).append(row)
        reached: set[str] = set()
        for track, mine in by_track.items():
            quotes = [self.quotes.get(r.symbol) for r in mine]
            if any(q is None for q in quotes):
                continue  # cannot value the whole ledger: no day-goal exit this pass
            acct = self.ledger(session, now, track)
            base = acct.goal_base or acct.day_start_equity
            if base <= 0:
                continue
            banked = acct.cash
            for r, q in zip(mine, quotes, strict=True):
                assert q is not None
                pos = self._to_sim(r)
                banked += r.margin + sim.unrealized(pos, q) - q.mid * pos.qty * fee_rate
            if banked >= base * (1 + take_profit):
                reached.add(track)
                acct.goal_base = banked
                if track in STORY_TRACKS:
                    goal_cycle.request(session, now, track)
                log.info(
                    "%s: day goal reached, equity %.2f from base %.2f; closing %d trade(s)",
                    track,
                    banked,
                    base,
                    len(mine),
                )
        return reached

    SHOCK_WINDOW = timedelta(minutes=15)
    SHOCK_CREDIBILITY = Decimal("0.6")
    SHOCK_ATR = Decimal(1)

    def _shock_against(self, session: Session, row: Position, quote: Quote, now: datetime) -> bool:
        """v10 (owner 2026-10-12): a credible, fresh news shock against the position in the
        last fifteen minutes, labelled by Jev, and the price has moved at least one ATR
        the same way since the post. Two independent facts, a label and a move, so no
        single model's word closes anything; the executor does, in code."""
        from app.db.models import XPostRecord

        if not row.atr or row.atr <= 0:
            return False
        against = "bearish" if row.direction == "long" else "bullish"
        post = session.execute(
            select(XPostRecord.created_at)
            .where(
                XPostRecord.asset == row.asset,
                XPostRecord.shock.is_(True),
                XPostRecord.fresh.is_not(False),
                XPostRecord.stance == against,
                XPostRecord.credibility >= self.SHOCK_CREDIBILITY,
                XPostRecord.created_at >= now - self.SHOCK_WINDOW,
            )
            .order_by(XPostRecord.created_at)
            .limit(1)
        ).scalar_one_or_none()
        if post is None:
            return False
        then = session.execute(
            select(Candle.close)
            .where(
                Candle.symbol == row.symbol,
                Candle.interval == "15",
                Candle.open_time <= post,
            )
            .order_by(Candle.open_time.desc())
            .limit(1)
        ).scalar_one_or_none()
        if then is None:
            return False
        moved = (then - quote.mid) if row.direction == "long" else (quote.mid - then)
        if moved >= self.SHOCK_ATR * row.atr:
            log.warning(
                "%s %s: news shock against the position, price %.4g -> %.4g (%.1f ATR): closing",
                row.mode,
                row.asset,
                then,
                quote.mid,
                moved / row.atr,
            )
            return True
        return False

    @staticmethod
    def _approved_while_in_trade(session: Session, d: DecisionRecord, track: str) -> bool:
        """Was a position in this asset open on this track when the decision was made?"""
        return bool(
            session.scalar(
                select(func.count())
                .select_from(Position)
                .where(
                    Position.track == track,
                    Position.asset == d.asset,
                    Position.opened_at <= d.ts,
                    (Position.closed_at.is_(None)) | (Position.closed_at >= d.ts),
                )
            )
        )

    def _rebase_flat_ledgers(self, session: Session, now: datetime, take_profit: Decimal) -> None:
        """A ledger with nothing open that already stands a goal above its base (a target
        or a single trade banked it) counts the next goal from where it is now. Otherwise
        the next trade would be closed the moment it opened."""
        busy = set(
            session.scalars(
                select(Position.track).where(Position.status.in_(["open", "closing"]))
            ).all()
        )
        for track in TRACKS[self.mode]:
            if track in busy:
                continue
            acct = self.ledger(session, now, track)
            base = acct.goal_base or acct.day_start_equity
            if base > 0 and acct.cash >= base * (1 + take_profit):
                acct.goal_base = acct.cash
                if track in STORY_TRACKS:
                    goal_cycle.request(session, now, track)

    def _take_profit(self, session: Session) -> Decimal | None:
        """The aggressive profile's profit level (a share of equity), or None when the
        mode is off."""
        level = self.cfg.profile.take_profit_equity_pct  # v14: the one mode, always on
        return level if level > 0 else None

    @staticmethod
    def _locked_sleeves(session: Session, now: datetime) -> dict[str, dict[str, Any]]:
        """Active day locks per sleeve: which directions are locked and since when."""
        from app.risk.sleeves import lock_info

        return {}  # v14: no locks of any kind
        state = session.get(RiskState, 1)
        locks = (state.day_locked_sleeves or {}) if state else {}
        out = {}
        for name in locks:
            info = lock_info(locks, name, now)
            if info is not None:
                out[name] = info
        return out

    def _sleeve_locked(self, row: Position, now: datetime) -> bool:
        """Should this position be closed by its sleeve's day-loss stop? Yes when it was
        open at the moment the stop fired; a trade opened afterwards in the direction that
        is still allowed stays."""
        sleeve = row.sleeve or self.cfg.sleeve_of(row.asset)
        info = self._locked_cache.get(sleeve) if sleeve is not None else None
        if info is None:
            return False
        since = info["since"]
        return since is None or row.opened_at is None or row.opened_at <= since

    def _live_sleeves(self, session: Session) -> set[str]:
        """Sleeves allowed to open for real. Each sleeve passes its own go-live evaluation;
        without any recorded verdict (the mode was flipped by hand) the process mode rules
        every sleeve."""
        state = session.get(RiskState, 1)
        modes = (state.sleeve_modes or {}) if state else {}
        if not modes:
            return set(self.cfg.sleeves)
        return {name for name, m in modes.items() if m == "live"}

    def _closes_15m(self, now: datetime) -> dict[str, Decimal]:
        """The close of the 15-minute candle that just ended, per symbol, on the first pass
        after a boundary; empty otherwise. The last mid seen before the boundary is the
        close; the bucket index tells a new candle from the running one."""
        bucket = int(now.timestamp() // 900)
        out: dict[str, Decimal] = {}
        for symbol, quote in self.quotes.items():
            prev = self._candle_ref.get(symbol)
            if prev is not None and prev[0] != bucket:
                out[symbol] = prev[1]
            self._candle_ref[symbol] = (bucket, quote.mid)
        return out

    @staticmethod
    def backup_trigger(row: Position) -> Decimal:
        """``BACKUP_ATR_MULT`` ATR beyond the stop the bot itself enforces on touch: the
        hard stop (or the trailing stop once it is armed and tighter). Without an ATR on
        the row, the stop distance stands in for it."""
        assert row.entry_price is not None
        touch = row.hard_stop if row.hard_stop is not None else row.stop
        atr = row.atr if row.atr and row.atr > 0 else abs(row.entry_price - touch)
        offset = BACKUP_ATR_MULT * atr
        if row.direction == "long":
            stop = max(touch, row.trail_stop) if row.trail_stop is not None else touch
            return stop - offset
        stop = min(touch, row.trail_stop) if row.trail_stop is not None else touch
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

    def store_marks(self, session: Session, now: datetime) -> None:
        """The latest quote per symbol into ``marks``, for the live feed. Never lets a
        write problem reach the trading loop."""
        from app.db.models import Mark

        try:
            for symbol, quote in self.quotes.items():
                session.merge(Mark(symbol=symbol, bid=quote.bid, ask=quote.ask, ts=quote.ts))
            session.commit()
        except Exception:
            session.rollback()
            log.exception("could not store marks")

    async def cancel_orphan_backups(self, session: Session, now: datetime) -> int:
        """Cancel backup stops on the exchange that no open position owns.

        A placement can land on the exchange while the call that made it reports a failure
        (a timeout): the bot then never records the link and places another, and the first
        one stays behind after the position is closed. Found on 2026-10-11: a buy stop for
        a DOGE short that had been closed hours earlier, which would have opened a position
        nobody asked for. Checked once a minute; an orphan is cancelled and reported."""
        gw = self.gateway
        if not getattr(gw, "supports_backup_stop", False) or not hasattr(
            gw, "resting_backup_stops"
        ):
            return 0
        if now - self._orphan_checked_at < timedelta(seconds=60):
            return 0
        self._orphan_checked_at = now
        try:
            resting = await gw.resting_backup_stops()
        except Exception as exc:  # a failed read must not disturb the trading loop
            log.warning("could not list backup stops: %s", exc)
            return 0
        owned = set(
            session.scalars(
                select(Position.backup_stop_link).where(
                    Position.status.in_(["open", "closing"]),
                    Position.backup_stop_link.is_not(None),
                )
            ).all()
        )
        cancelled = 0
        for symbol, link in resting:
            if link in owned:
                continue
            # a stop placed this very minute may not be recorded yet: leave it one round
            if link in self._orphan_seen:
                if await gw.cancel_backup_stop(symbol, link):
                    cancelled += 1
                    self._orphan_seen.discard(link)
                    log.warning("%s: cancelled an orphan backup stop %s", symbol, link)
                    self._alert(
                        f"dorkbot: cancelled a leftover stop order on {symbol}",
                        f"A backup stop ({link}) was resting on Bybit for {symbol} with no "
                        "open position behind it. The executor cancelled it.",
                    )
            else:
                self._orphan_seen.add(link)
        return cancelled

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
        take_profit = self._take_profit(session)
        for track, track_mode in TRACKS[self.mode].items():
            if take_profit is not None and self._at_day_goal(session, now, track, take_profit):
                # Owner 2026-10-15: an ENA short lived 18 seconds before the day-goal sweep
                # closed it for its fees. Nothing opens while the book stands at the goal.
                continue
            await self._open_track(session, now, track, track_mode)

    def _at_day_goal(
        self, session: Session, now: datetime, track: str, take_profit: Decimal
    ) -> bool:
        acct = self.ledger(session, now, track)
        base = acct.goal_base or acct.day_start_equity
        return base > 0 and acct.equity >= base * (1 + take_profit)

    def pilot_slot(self, asset: str) -> Decimal:
        """The pilot's size for one position. Like the paper book, a position takes a share
        of its sleeve (owner 2026-10-11: the pilot should be able to take every trade), so
        all ten assets fit at once: 14% of the pilot's capital for a bluechip, 6% for an
        alt. An asset outside the sleeves keeps the old share of the whole."""
        # v14: one book; every asset gets the same slot, and all of them fit at once
        n = max(1, len(self.cfg.trading.assets))
        share = min(self.cfg.trading.capital_share_per_asset, Decimal(1) / n)
        return self.cfg.pilot.capital_usdt * share

    @staticmethod
    def _note(d: DecisionRecord, track: str, text: str) -> None:
        """Why this track did not open the approved plan, for the decision log."""
        notes = dict(d.exec_notes or {})
        if notes.get(track) != text:
            notes[track] = text
            d.exec_notes = notes

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
        locked = self._locked_sleeves(session, now)
        live_sleeves = self._live_sleeves(session)
        for d in rows:
            if d.asset in seen:
                continue
            seen.add(d.asset)
            if session.scalar(
                select(func.count())
                .select_from(Position)
                .where(Position.decision_id == d.id, Position.track == track)
            ):
                continue  # already executed
            if d.asset in open_assets:
                self._note(d, track, f"already in a {d.asset} trade")
                continue
            if self._approved_while_in_trade(session, d, track):
                # Owner 2026-10-15: the plan was approved as "nothing to add" while a
                # trade in this asset was open; it does not become a fresh entry at the
                # price that trade just closed on. The next cycle judges the market anew.
                self._note(d, track, f"approved while in a {d.asset} trade; not re-entered")
                continue
            sleeve = self.cfg.sleeve_of(d.asset)
            lock = locked.get(sleeve) if sleeve is not None else None
            if lock is not None and d.direction in lock["dirs"]:
                # the sleeve's day-loss stop: nothing new in the direction that lost
                self._note(d, track, f"new {d.direction}s locked for the day (loss stop)")
                continue
            if track_mode == "live" and sleeve is not None and sleeve not in live_sleeves:
                # this sleeve has not passed its own go-live evaluation yet
                self._note(d, track, "sleeve not live yet")
                continue
            plan = (d.proposal or {}).get(plan_key)
            symbol = self.cfg.symbol(d.asset)
            quote = self.quotes.get(symbol)
            if not plan or quote is None:
                continue
            side = Side.LONG if d.direction == "long" else Side.SHORT
            notional, leverage = Decimal(plan["notional"]), Decimal(plan["leverage"])
            if track == "pilot":  # owner: 200 USDT at 1x, to test fills, slippage, borrowing
                leverage = Decimal(1)
                notional = min(notional, acct.cash, self.pilot_slot(d.asset))
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
                    "hard_stop": str(Decimal(plan.get("hard_stop", plan["stop"])) + offset),
                    "target": str(Decimal(plan["target"]) + offset),
                }
                log.info("%s: entry clamped to spot (%+.2f%% from plan)", d.asset, drift * 100)
            if track == "inverse":
                # The mirror image: the other side, the same distances the other way round.
                side = Side.SHORT if side is Side.LONG else Side.LONG
                entry = Decimal(plan["entry"])
                plan = {
                    **plan,
                    "stop": str(entry + (entry - Decimal(plan["stop"]))),
                    "hard_stop": str(
                        entry + (entry - Decimal(plan.get("hard_stop", plan["stop"])))
                    ),
                    "target": str(entry - (Decimal(plan["target"]) - entry)),
                }
            if notional / leverage > acct.cash:
                # The last asset to open finds the cash already committed as margin by the
                # others (five shares of 20%, fees paid). Size down to what is free rather
                # than skip; the trade's risk only ever shrinks. Below a quarter of the
                # plan it is not worth a trade.
                fit = (acct.cash * leverage * CASH_FIT_BUFFER).quantize(Decimal("0.01"))
                key = f"cash:{track}:{d.id}"
                if fit < notional * MIN_FIT_FRACTION:
                    if key not in self._alerted:
                        self._alerted.add(key)
                        log.warning(
                            "%s (%s): margin %s exceeds free cash %s; skipped until cash frees",
                            d.asset,
                            track,
                            notional / leverage,
                            acct.cash,
                        )
                    self._note(d, track, "out of funds")
                    continue
                log.info("%s (%s): sized down to free cash: %s → %s", d.asset, track, notional, fit)
                notional = fit
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
                    hard_stop=Decimal(plan["hard_stop"]) if plan.get("hard_stop") else None,
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
                why = "borrow failed" if result.outcome is Outcome.BORROW_FAILED else "rejected"
                self._note(d, track, f"{why} by the exchange: {str(result.detail)[:70]}")
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
                sleeve=self.cfg.sleeve_of(d.asset),
                status="open",
                qty=pos.qty,
                entry_price=pos.entry,
                leverage=pos.leverage,
                margin=pos.margin,
                notional=pos.qty * pos.entry,
                borrowed=pos.borrowed,
                stop=pos.stop,
                hard_stop=pos.hard_stop,
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
            # The margin leaves cash now; the entry fee does not: it sits in the position
            # and comes out of the net result at the close (until 2026-10-26 it was taken
            # here as well, so every round trip cost its entry fee twice in the ledger)
            acct.cash -= pos.margin
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
        storyline = "live" if self.mode == "live" else "primary"
        if row.mode == "pilot" or row.track != storyline:
            return  # one track tells the X story: live once live, the primary paper track before
        events = self.cfg.posting.events
        if kind == "open" and events != "all":
            return
        if kind == "close" and events == "wins" and not (row.pnl is not None and row.pnl > 0):
            return
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
            hard_stop=row.hard_stop,
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

    def _warn_near_liquidation(self, row: Position, pos: sim.PaperPosition, quote: Quote) -> None:
        """One mail per position when the price has covered most of the way from the entry
        to the liquidation level (owner, 2026-10-09). The guard closes before the level."""
        left = sim.liquidation_room(pos, quote)
        key = f"liq:{row.id}"
        if left is None or left > LIQ_WARN_ROOM or key in self._alerted:
            return
        self._alerted.add(key)
        self._alert(
            f"dorkbot: {row.asset} ({row.track}) is near liquidation",
            f"{row.mode} {row.track} {row.asset} {row.direction} {float(row.leverage):g}x: "
            f"price {quote.mid} has covered {1 - left:.0%} of the way from the entry "
            f"{row.entry_price} to the liquidation level {row.liquidation_price}. The stop sits "
            f"at {row.stop}; the guard closes before the level. "
            + ("Paper money." if row.mode == "paper" else "Real money."),
        )

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
        # v14: one mode, no locks. Only the brake closes everything: set by hand, by a
        # reconcile mismatch, or by the loss limit against starting capital
        # (trading.emergency_brake). A kill switch freezes new entries on its own path.
        subject = "dorkbot: EMERGENCY BRAKE, waiting for you"
        if state.emergency_brake and "brake" not in self._alerted:
            self._alerted.add("brake")
            self._alert(
                subject,
                f"{subject}. Reason: {state.brake_reason or 'risk rule'}. Mode {self.mode}. "
                "Resume from the admin when you are ready.",
            )
        if not state.emergency_brake:
            self._alerted.discard("brake")
        if state.emergency_brake:
            self.frozen = True
            return True
        return False

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
            await self.cancel_orphan_backups(session, now)
            self.store_marks(session, now)
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
