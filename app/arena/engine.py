"""The minute loop (bot spec §2, §3): after every closed 1-minute candle, for each active
trader, Jev judges the rulebook and the rules engine decides. Rounds, benches and the ledger
live here. No model ever places an order: the engine does, on Jev's label and the rules.

The engine owns one exchange adapter per trader (real or paper), one candle buffer (the
market is shared) and the strategy authors' output. State the exchange is the source of
truth for (the position, closed trades, equity) is re-read every minute, never assumed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.arena import author, conditions, ledger, reflex
from app.arena import rules as R
from app.arena.candles import CandleBuffer
from app.arena.exchange import ClosedTrade, Exchange, Position
from app.arena.paper import PaperExchange
from app.arena.rulebook import QUESTIONS_VERSION, Rulebook
from app.config import ArenaConfig
from app.db.models import ArenaBenchEvent, ArenaDecision, ArenaEquity, ArenaRulebook, ArenaTrade
from app.db.session import new_session

log = logging.getLogger("arena")
NAMES = {"kwikton": "Kwikton", "kwekton": "Kwekton", "kwakton": "Kwakton"}


@dataclass
class Trader:
    id: str
    model: str
    exchange: Exchange
    rulebook: Rulebook | None = None
    plan: R.Plan | None = None
    losses: int = 0
    benched: bool = False
    start_equity: Decimal | None = None
    pending_exit: str | None = None  # earlyExit | bench: the reason for a close we sent
    author_failed_at: datetime | None = None  # retried hourly (2026-10-18: Astra out of credits)
    last_check: datetime = field(default_factory=lambda: datetime.now(UTC))
    booked: set[str] = field(default_factory=set)  # closes booked already, by order id
    funded: bool = False

    @property
    def name(self) -> str:
        return NAMES.get(self.id, self.id.capitalize())


class Engine:
    def __init__(
        self,
        cfg: ArenaConfig,
        traders: dict[str, Trader],
        candles: CandleBuffer,
        *,
        jev_model: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.rules = R.Rules(
            max_leverage=cfg.max_leverage,
            tp_min=cfg.tp_net_roe_range[0],
            tp_max=cfg.tp_net_roe_range[1],
            losses_to_bench=cfg.losses_to_bench,
            taker_fee_pct=cfg.taker_fee_pct,
            min_conviction_floor=cfg.min_conviction_floor,
        )
        self.traders = traders
        self.candles = candles
        self.jev_model = jev_model or cfg.jev_model
        self.round_number = 0
        self.round_started: datetime | None = None
        self._authors: set[asyncio.Task[None]] = set()

    def _spawn_authors(self, traders: list[Trader], now: datetime) -> None:
        """The authors write in the background (Grok takes minutes): the minute loop goes
        on, and a trader trades as soon as its own book lands."""
        for t in traders:
            task = asyncio.create_task(self.write_rulebook(t, now))
            self._authors.add(task)
            task.add_done_callback(self._authors.discard)

    async def wait_authors(self) -> None:
        if self._authors:
            await asyncio.gather(*self._authors, return_exceptions=True)

    # --- lifecycle -------------------------------------------------------------------

    async def start(self, now: datetime | None = None) -> None:
        """Register the traders, pick up the open round and its state, or open round 1."""
        now = now or datetime.now(UTC)
        with new_session() as s:
            for t in self.traders.values():
                row = ledger.ensure_trader(s, t.id, t.name, t.model, now)
                t.start_equity = row.start_equity
            rnd = ledger.current_round(s)
            if rnd is None:
                s.commit()
                await self.new_round(now)
                return
            self.round_number, self.round_started = rnd.number, rnd.started_at
            for t in self.traders.values():
                t.losses = ledger.losses_in_round(s, t.id, rnd.number)
                t.benched = ledger.benched_in_round(s, t.id, rnd.number) is not None
                rb = ledger.latest_rulebook(s, t.id, rnd.number)
                if rb is not None and rb.plan:
                    t.rulebook = Rulebook.model_validate(
                        rb.plan.get("source")
                        or {k: v for k, v in rb.rulebook.items() if k != "conditions"}
                    )
                    t.plan = R.Plan(
                        leverage=int(rb.plan["leverage"]),
                        tp_net_roe_pct=Decimal(str(rb.plan["tp_net_roe_pct"])),
                        sl_net_roe_pct=Decimal(str(rb.plan["sl_net_roe_pct"])),
                        min_conviction=float(rb.plan["min_conviction"]),
                    )
                open_row = ledger.open_trade(s, t.id)
                if open_row is not None:
                    t.last_check = min(t.last_check, open_row.opened_at)
                if isinstance(t.exchange, PaperExchange):
                    self._restore_paper(s, t, open_row)
            s.commit()
        log.info("arena: round %d continues", self.round_number)
        # A restart during the authors' work, or a refused book: anyone without a book in
        # this round writes one now rather than sitting out until the next reset.
        missing = [t for t in self.traders.values() if t.rulebook is None]
        if missing:
            self._spawn_authors(missing, now)

    async def tick(self, now: datetime | None = None) -> None:
        """One minute: candles, closes, benches, the round, entries and exits, equity."""
        now = now or datetime.now(UTC)
        new_candle = await self.candles.refresh()
        last = self.candles.last_1m
        for t in self.traders.values():
            if isinstance(t.exchange, PaperExchange):
                t.exchange.now = now
                if last and new_candle:
                    await t.exchange.on_candle(Decimal(str(last[2])), Decimal(str(last[3])))
            await self.reconcile(t, now)
        requested = self._round_requested(now)
        running = (now - self.round_started).total_seconds() / 3600 if self.round_started else None
        if requested or R.round_over(
            {t.id: t.benched for t in self.traders.values()},
            hours_running=running,
            max_hours=self.cfg.max_round_hours,
        ):
            if not all(t.benched for t in self.traders.values()):
                log.info(
                    "arena: round %d ends %s",
                    self.round_number,
                    "on the owner's request"
                    if requested
                    else f"on the {self.cfg.max_round_hours}h limit",
                )
                for t in self.traders.values():
                    if await t.exchange.position() is not None:
                        t.pending_exit = "bench"
                        await t.exchange.close_market(f"arena-{t.id}-round-{int(now.timestamp())}")
                        await self.reconcile(t, now)
            await self.new_round(now)
        # an author that failed (a provider out of credits, a timeout) is retried hourly
        retry = [
            t
            for t in self.traders.values()
            if t.rulebook is None
            and t.author_failed_at is not None
            and now - t.author_failed_at >= timedelta(hours=1)
            and not any(not task.done() for task in self._authors)
        ]
        if retry:
            for t in retry:
                t.author_failed_at = now
            self._spawn_authors(retry, now)
        if new_candle:
            for t in self.traders.values():
                if t.benched or t.plan is None or t.rulebook is None:
                    continue
                try:
                    await self.decide(t, now)
                except Exception:  # one trader's problem never stops the others
                    log.exception("arena %s: decision failed", t.id)
        await self.record_equity(now)

    def _restore_paper(self, s: Any, t: Trader, open_row: ArenaTrade | None) -> None:
        """The paper twin's cash and position from the ledger: the allocation plus every
        closed trade's net result, minus the open trade's margin and entry fee."""
        from sqlalchemy import func as fn

        assert isinstance(t.exchange, PaperExchange)
        realized = s.execute(
            select(fn.coalesce(fn.sum(ArenaTrade.pnl_usdt), 0)).where(
                ArenaTrade.trader == t.id, ArenaTrade.status == "closed"
            )
        ).scalar_one()
        start = t.start_equity or t.exchange.cash
        cash = start + Decimal(str(realized))
        position = None
        open_fee = Decimal(0)
        if open_row is not None:
            open_fee = open_row.entry_price * open_row.qty * t.exchange.fee
            cash -= open_row.margin_usdt + open_fee
            position = Position(
                side=open_row.side,
                qty=open_row.qty,
                entry=open_row.entry_price,
                leverage=open_row.leverage,
                tp=open_row.tp_price,
                sl=open_row.sl_price,
                unrealized_pnl=Decimal(0),
                margin=open_row.margin_usdt,
            )
        t.exchange.restore(cash, position, open_fee)
        if position is not None:
            log.info(
                "arena %s: paper position restored (%s at %s)", t.id, position.side, position.entry
            )

    # --- rounds ------------------------------------------------------------------------

    def _round_requested(self, now: datetime) -> bool:
        """The owner's `python -m app.control arena-round`: end the round now."""
        from app.db.models import ControlRequest

        with new_session() as s:
            rows = s.scalars(
                select(ControlRequest).where(
                    ControlRequest.kind == "arena_round", ControlRequest.applied_at.is_(None)
                )
            ).all()
            for r in rows:
                r.applied_at, r.result = now, f"round {self.round_number} ended, new rulebooks"
            s.commit()
            return bool(rows)

    async def new_round(self, now: datetime) -> None:
        with new_session() as s:
            cur = ledger.current_round(s)
            if cur is not None:
                winner = self._round_winner(s, cur.started_at, now)
                ledger.end_round(s, cur, now, winner)
                log.info("arena: round %d over, winner %s", cur.number, winner)
            rnd = ledger.start_round(s, now)
            s.commit()
            self.round_number, self.round_started = rnd.number, rnd.started_at
        for t in self.traders.values():
            t.losses, t.benched, t.rulebook, t.plan = 0, False, None, None
        log.info("arena: round %d starts", self.round_number)
        self._spawn_authors(list(self.traders.values()), now)

    def _round_winner(self, s: Any, started: datetime, now: datetime) -> str | None:
        best: tuple[Decimal, str] | None = None
        for t in self.traders.values():
            start = ledger.equity_at(s, t.id, started)
            end = ledger.equity_at(s, t.id, now)
            if start is None or end is None or start <= 0:
                continue
            delta = end / start - 1
            if best is None or delta > best[0]:
                best = (delta, t.id)
            elif delta == best[0]:
                best = (delta, "")  # a tie has no winner
        # no winner without a gain: a round nobody traded is nobody's (2026-10-15)
        return best[1] if best and best[1] and best[0] > 0 else None

    async def write_rulebook(self, t: Trader, now: datetime) -> None:
        with new_session() as s:
            board = ledger.leaderboard_view(s, now)
            stats: dict[str, Any] = next((x for x in board["traders"] if x["id"] == t.id), {})
            prev = ledger.latest_rulebook(s, t.id, self.round_number - 1)
            prev_rb = prev.rulebook if prev else None
            prev_result = self._round_result(s, t.id, self.round_number - 1) if prev else None
        context = author.round_context(
            trader=t.name,
            round_number=self.round_number,
            stats=stats,
            last_rulebook=prev_rb,
            last_result=prev_result,
            hourly=self.candles.frames["1h"],
            quarter_hourly=self.candles.frames["15m"],
        )
        rb = await author.write_rulebook(t.model, context, round_number=self.round_number)
        plan: R.Plan | None = None
        if rb is not None:
            try:
                plan = R.plan_for(
                    self.rules, rb.leverage, rb.tp_net_roe_pct, rb.sl_net_roe_pct, rb.min_conviction
                )
            except R.RulesError as exc:
                log.warning("arena %s: rulebook refused: %s", t.id, exc)
        t.rulebook, t.plan = rb, plan
        t.author_failed_at = now if rb is None else None
        with new_session() as s:
            s.add(
                ArenaRulebook(
                    trader=t.id,
                    round=self.round_number,
                    rulebook=rb.public() if rb else {},
                    plan=(
                        {
                            "leverage": plan.leverage,
                            "tp_net_roe_pct": str(plan.tp_net_roe_pct),
                            "sl_net_roe_pct": str(plan.sl_net_roe_pct),
                            "min_conviction": plan.min_conviction,
                            "notes": list(plan.notes),
                            "source": rb.model_dump(mode="json") if rb else None,
                        }
                        if plan
                        else None
                    ),
                    questions_version=QUESTIONS_VERSION,
                    model=t.model,
                    created_at=now,
                )
            )
            s.commit()
        if rb and plan:
            log.info(
                "arena %s: %r %dx TP %s SL %s",
                t.id,
                rb.name,
                plan.leverage,
                plan.tp_net_roe_pct,
                plan.sl_net_roe_pct,
            )

    def _round_result(self, s: Any, trader: str, round_number: int) -> dict[str, Any] | None:
        from sqlalchemy import select

        rows = s.scalars(
            select(ArenaTrade).where(
                ArenaTrade.trader == trader,
                ArenaTrade.round == round_number,
                ArenaTrade.status == "closed",
            )
        ).all()
        if not rows:
            return {"trades": 0}
        rets = [float(x.net_roe_pct or 0) for x in rows]
        return {
            "trades": len(rows),
            "wins": sum(1 for r in rets if r > 0),
            "net_roe_pct_sum": round(sum(rets), 2),
            "benched": ledger.benched_in_round(s, trader, round_number) is not None,
        }

    # --- the exchange's truth ----------------------------------------------------------

    async def reconcile(self, t: Trader, now: datetime) -> None:
        """Book every close the exchange reports since the last check; count losses and
        bench. A benched trader with a position still open is closed at market."""
        closed = await t.exchange.closed_trades(t.last_check)
        for c in closed:
            # `closed_trades(since)` is inclusive, so a fill at the second of the last check
            # comes back once more; booking it again closed the next trade (2026-10-23)
            if c.order_id in t.booked:
                continue
            t.booked.add(c.order_id)
            await self.book_close(t, c, now)
        if len(t.booked) > 1000:
            t.booked = set(list(t.booked)[-500:])
        t.last_check = now
        if t.benched:
            pos = await t.exchange.position()
            if pos is not None:
                t.pending_exit = "bench"
                await t.exchange.close_market(f"arena-{t.id}-bench-{int(now.timestamp())}")

    async def book_close(self, t: Trader, c: ClosedTrade, now: datetime) -> None:
        with new_session() as s:
            row = ledger.open_trade(s, t.id)
            if row is None:
                log.warning("arena %s: close %s with no open trade on record", t.id, c.order_id)
                return
            row.exit_price, row.closed_at, row.fees_usdt, row.pnl_usdt = (
                c.exit,
                c.closed_at,
                c.fees,
                c.pnl,
            )
            row.net_roe_pct = R.net_roe_pct(c.pnl, row.margin_usdt)
            if t.pending_exit:
                row.exit_reason, t.pending_exit = t.pending_exit, None
            else:
                near_tp = abs(c.exit - row.tp_price) <= abs(c.exit - row.sl_price)
                row.exit_reason = "tp" if near_tp else "sl"
            row.status = "closed"
            loss = R.is_loss(row.net_roe_pct)
            if loss:
                t.losses += 1
            log.info(
                "arena %s: closed %s %s at %s, net %s%% (%s)%s",
                t.id,
                row.side,
                row.qty,
                c.exit,
                row.net_roe_pct,
                row.exit_reason,
                f", loss {t.losses}/{self.rules.losses_to_bench}" if loss else "",
            )
            if loss and R.is_benched(t.losses, self.rules) and not t.benched:
                t.benched = True
                s.add(
                    ArenaBenchEvent(
                        trader=t.id, round=self.round_number, at=c.closed_at, loss_count=t.losses
                    )
                )
                log.info("arena %s: benched in round %d", t.id, self.round_number)
            s.commit()

    # --- decisions ---------------------------------------------------------------------

    async def decide(self, t: Trader, now: datetime) -> None:
        assert t.rulebook is not None and t.plan is not None
        pos = await t.exchange.position()
        if pos is None:
            with new_session() as s:
                if ledger.open_trade(s, t.id) is not None:
                    return  # the exchange closed it; the next reconcile books it
            if not t.funded:
                return
            await self.maybe_enter(t, now)
        elif t.pending_exit is None:
            await self.maybe_exit(t, pos, now)

    async def maybe_enter(self, t: Trader, now: datetime) -> None:
        assert t.rulebook is not None and t.plan is not None
        rb = t.rulebook
        if rb.long_conditions or rb.short_conditions:
            # 2026-10-15: the structured rules, checked exactly in code every minute.
            frames = self.candles.frames
            block = conditions.evaluate(frames, rb.no_trade_conditions)
            longs = conditions.evaluate(frames, rb.long_conditions)
            shorts = conditions.evaluate(frames, rb.short_conditions)
            blocked = block["count"] > 0 and block["count_met"] > 0
            choice = "none"
            if not blocked and longs["met"] and not shorts["met"]:
                choice = "long"
            elif not blocked and shorts["met"] and not longs["met"]:
                choice = "short"
            sig = reflex.Signal(
                choice=choice,
                probabilities={
                    "long": round(longs["count_met"] / longs["count"], 2)
                    if longs["count"]
                    else 0.0,
                    "short": round(shorts["count_met"] / shorts["count"], 2)
                    if shorts["count"]
                    else 0.0,
                    "none": 1.0 if choice == "none" else 0.0,
                    "blocked": float(block["count_met"]),
                },
                conviction=1.0 if choice != "none" else 0.0,
            )
            act = choice != "none"
        else:
            state = self.candles.state(None, now)
            jev = await reflex.decide_entry(rb, state, model=self.jev_model, trader=t.id)
            if jev is None:
                return
            sig = jev
            act = sig.choice in ("long", "short") and sig.conviction >= t.plan.min_conviction
        ts = self.candles.last_1m[0] if self.candles.last_1m else now.timestamp() * 1000
        with new_session() as s:
            d = ArenaDecision(
                trader=t.id,
                round=self.round_number,
                candle_ts=datetime.fromtimestamp(ts / 1000, tz=UTC),
                kind="signal",
                probabilities=sig.probabilities,
                conviction=sig.conviction,
                min_conviction=t.plan.min_conviction,
                acted=act,
                created_at=now,
            )
            s.add(d)
            s.commit()
            decision_id = d.id
        if not act:
            return
        await self.open(t, sig.choice, decision_id, now)

    async def open(self, t: Trader, side: str, decision_id: int, now: datetime) -> None:
        assert t.plan is not None
        plan, ex = t.plan, t.exchange
        inst = await ex.instrument()
        _, available = await ex.equity()
        price = await ex.last_price()
        margin = available * self.cfg.margin_share
        qty = R.qty_for(margin, plan.leverage, price, inst.lot, inst.min_qty)
        if qty <= 0:
            log.warning("arena %s: no size for %s USDT at %s", t.id, margin, price)
            return
        await ex.set_leverage(plan.leverage)
        tp = R.tp_price(self.rules, plan, price, side, inst.tick)
        sl = R.sl_price(self.rules, plan, price, side, inst.tick)
        link = f"arena-{t.id}-{self.round_number}-{int(now.timestamp())}"
        await ex.open_market(side, qty, tp, sl, link)
        pos = await ex.position()
        entry = pos.entry if pos is not None and pos.entry > 0 else price
        if pos is not None and pos.entry != price:
            tp = R.tp_price(self.rules, plan, entry, side, inst.tick)
            sl = R.sl_price(self.rules, plan, entry, side, inst.tick)
            await ex.set_tpsl(tp, sl)
        filled_qty = pos.qty if pos is not None else qty
        with new_session() as s:
            s.add(
                ArenaTrade(
                    trader=t.id,
                    round=self.round_number,
                    decision_id=decision_id,
                    side=side,
                    leverage=plan.leverage,
                    qty=filled_qty,
                    entry_price=entry,
                    tp_price=tp,
                    sl_price=sl,
                    tp_net_roe=plan.tp_net_roe_pct,
                    sl_net_roe=plan.sl_net_roe_pct,
                    margin_usdt=(
                        pos.margin
                        if pos is not None and pos.margin > 0
                        else entry * filled_qty / plan.leverage
                    ),
                    opened_at=now,
                    order_link_id=link,
                    status="open",
                )
            )
            s.commit()
        log.info(
            "arena %s: opened %s %s at %s, %dx, tp %s sl %s",
            t.id,
            side,
            filled_qty,
            entry,
            plan.leverage,
            tp,
            sl,
        )

    async def maybe_exit(self, t: Trader, pos: Position, now: datetime) -> None:
        assert t.rulebook is not None and t.plan is not None
        rb = t.rulebook
        if rb.exit_conditions or rb.max_hold_minutes:
            with new_session() as s:
                row = ledger.open_trade(s, t.id)
            minutes = int((now - row.opened_at).total_seconds() // 60) if row else 0
            res = conditions.evaluate(self.candles.frames, rb.exit_conditions)
            hit = res["count"] > 0 and res["count_met"] > 0
            timed = rb.max_hold_minutes is not None and minutes >= rb.max_hold_minutes
            if hit or timed:
                t.pending_exit = "earlyExit"
                await t.exchange.close_market(f"arena-{t.id}-exit-{int(now.timestamp())}")
                log.info("arena %s: early exit (%s)", t.id, "rule" if hit else f"{minutes} min")
            return
        if not rb.early_exit:
            return
        with new_session() as s:
            row = ledger.open_trade(s, t.id)
        minutes_open = int((now - row.opened_at).total_seconds() // 60) if row else None
        roe = float(pos.unrealized_pnl / pos.margin * 100) if pos.margin else 0.0
        state = self.candles.state(
            {
                "side": pos.side,
                "entry": float(pos.entry),
                "leverage": pos.leverage,
                "unrealized_roe_pct": round(roe, 2),
                "minutes_open": minutes_open,
            },
            now,
        )
        e = await reflex.decide_exit(
            t.rulebook, state, side=pos.side, model=self.jev_model, trader=t.id
        )
        if e is None:
            return
        act = e.close and e.probability >= t.plan.min_conviction
        ts = self.candles.last_1m[0] if self.candles.last_1m else now.timestamp() * 1000
        with new_session() as s:
            s.add(
                ArenaDecision(
                    trader=t.id,
                    round=self.round_number,
                    candle_ts=datetime.fromtimestamp(ts / 1000, tz=UTC),
                    kind="early_exit",
                    probabilities={"true": e.probability},
                    conviction=e.probability,
                    min_conviction=t.plan.min_conviction,
                    acted=act,
                    created_at=now,
                )
            )
            s.commit()
        if act:
            t.pending_exit = "earlyExit"
            await t.exchange.close_market(f"arena-{t.id}-exit-{int(now.timestamp())}")

    # --- equity --------------------------------------------------------------------------

    async def record_equity(self, now: datetime) -> None:
        with new_session() as s:
            for t in self.traders.values():
                try:
                    equity, _ = await t.exchange.equity()
                except Exception as exc:
                    log.warning("arena %s: equity unavailable: %s", t.id, exc)
                    continue
                t.funded = equity > 0
                if t.start_equity is None and equity > 0:
                    row = s.get(type(ledger.ensure_trader(s, t.id, t.name, t.model, now)), t.id)
                    assert row is not None
                    row.start_equity = equity
                    t.start_equity = equity
                if not t.start_equity:
                    continue
                index = (equity / t.start_equity * 100).quantize(Decimal("0.0001"))
                s.merge(ArenaEquity(trader=t.id, ts=now, equity_usdt=equity, equity_index=index))
            s.commit()

    def positions_public(
        self, positions: dict[str, Position | None], now: datetime
    ) -> dict[str, dict[str, Any] | None]:
        """The open positions in the site's shape, unrealised ROE on the margin."""
        out: dict[str, dict[str, Any] | None] = {}
        for tid, p in positions.items():
            if p is None:
                out[tid] = None
                continue
            roe = float(p.unrealized_pnl / p.margin * 100) if p.margin else 0.0
            with new_session() as s:
                row = ledger.open_trade(s, tid)
            out[tid] = {
                "side": p.side,
                "entry": float(p.entry),
                "leverage": p.leverage,
                "tp": float(p.tp or 0),
                "sl": float(p.sl or 0),
                "unrealized_roe_pct": round(roe, 2),
                "opened_at": (row.opened_at if row else now).isoformat(),
            }
        return out
