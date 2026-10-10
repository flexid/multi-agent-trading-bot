"""Go-live checker, leverage ramp and capital ramp (SPEC §10, §8; owner rules 2026-10-09).

The bot decides itself. ``evaluate`` returns every criterion with its value and pass/fail
on the primary paper track; ``decide`` flips ``risk_state.mode`` to live only when all
pass AND ``trading.live_allowed`` is true AND the live self-test has passed. The leverage
ramp moves ``risk_state.leverage_ceiling`` 2x → 5x → 10x and back down on
underperformance; the capital ramp moves ``risk_state.capital_fraction`` 10% → 25% → 50%
→ 100% of ``capital_max_usdt`` and one step back after a drawdown pause.

"Net" everywhere in this module means after ALL costs: trading P&L net of fees and
borrow interest, minus what the bot spends on itself (LLM, X, server; ``app.costs``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config
from app.costs import running_costs
from app.db.models import (
    Candle,
    EquitySnapshot,
    FetchRun,
    PaperAccount,
    Position,
    RiskState,
)

MIN_SHADOW_DAYS = 28
MIN_CLOSED_TRADES = 100
MIN_PROFIT_FACTOR = Decimal("1.3")
MAX_DRAWDOWN = Decimal("0.10")
INCIDENT_WINDOW_DAYS = 14
INCIDENT_ERRORS_IN_ROW = 6  # 90 minutes of a source failing counts as an incident
CAPITAL_STEPS = (Decimal("0.25"), Decimal("0.50"), Decimal("1.00"))  # after the start fraction
CAPITAL_STEP_DAYS = 21


@dataclass(frozen=True)
class Criterion:
    name: str
    value: str
    ok: bool


@dataclass(frozen=True)
class Verdict:
    ready: bool
    criteria: list[Criterion]
    checked_at: datetime


def _closed(
    session: Session, mode: str = "paper", assets: list[str] | None = None
) -> list[Position]:
    stmt = select(Position).where(
        Position.track.in_(["primary", "live"]),
        Position.status == "closed",
        Position.mode == mode,
    )
    if assets is not None:
        stmt = stmt.where(Position.asset.in_(assets))
    return list(session.scalars(stmt).all())


def _equity_series(session: Session, mode: str = "paper:primary") -> list[tuple[datetime, Decimal]]:
    rows = session.execute(
        select(EquitySnapshot.ts, EquitySnapshot.equity)
        .where(EquitySnapshot.mode == mode, EquitySnapshot.equity > 0)
        .order_by(EquitySnapshot.ts)
    ).all()
    return [(r.ts, Decimal(str(r.equity))) for r in rows]


def sharpe(returns: list[float], periods_per_year: float) -> float | None:
    if len(returns) < 10:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    sd = math.sqrt(var)
    return (mean / sd) * math.sqrt(periods_per_year) if sd > 1e-9 else None


def daily_returns(series: list[tuple[datetime, Decimal]]) -> list[float]:
    by_day: dict[str, Decimal] = {}
    for ts, eq in series:
        by_day[ts.strftime("%Y-%m-%d")] = eq  # last snapshot of the day
    vals = [float(v) for _, v in sorted(by_day.items())]
    return [b / a - 1 for a, b in zip(vals, vals[1:], strict=False) if a > 0]


def btc_daily_returns(session: Session, cfg: Config, since: datetime) -> list[float]:
    rows = (
        session.execute(
            select(Candle.close)
            .where(
                Candle.symbol == cfg.symbol("BTC"),
                Candle.interval == "D",
                Candle.open_time >= since,
            )
            .order_by(Candle.open_time)
        )
        .scalars()
        .all()
    )
    vals = [float(v) for v in rows]
    return [b / a - 1 for a, b in zip(vals, vals[1:], strict=False)]


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _sleeve_series(
    session: Session, cfg: Config, sleeve: str, since: datetime
) -> list[tuple[datetime, Decimal]]:
    """A sleeve's equity curve from its realized P&L: its share of starting capital plus
    the cumulative P&L of its closed trades, one point per close."""
    s = cfg.sleeves[sleeve]
    acct = session.get(PaperAccount, 1)
    start = (acct.starting_capital if acct else cfg.trading.capital_max_usdt) * s.capital_fraction
    rows = sorted(
        (p for p in _closed(session, assets=s.assets) if p.closed_at),
        key=lambda p: p.closed_at or since,
    )
    out, eq = [(since, start)], start
    for p in rows:
        eq += p.pnl or Decimal(0)
        out.append((p.closed_at or since, eq))
    return out


def evaluate(
    session: Session, cfg: Config, now: datetime | None = None, sleeve: str | None = None
) -> Verdict:
    """The go-live criteria on the primary paper record; with ``sleeve``, on that sleeve's
    trades and its own equity curve (owner 2026-10-10: each sleeve is evaluated alone)."""
    now = now or datetime.now(UTC)
    book = _equity_series(session)
    start = book[0][0] if book else now
    series = _sleeve_series(session, cfg, sleeve, start) if sleeve else book
    days = (now - start).total_seconds() / 86400
    closed = _closed(session, assets=cfg.sleeves[sleeve].assets if sleeve else None)
    wins = sum(float(p.pnl) for p in closed if p.pnl and p.pnl > 0)
    losses = -sum(float(p.pnl) for p in closed if p.pnl and p.pnl < 0)
    pf = Decimal(str(wins / losses)) if losses > 0 else (Decimal(99) if wins > 0 else Decimal(0))
    acct = session.get(PaperAccount, 1)
    trading_net = (acct.equity - acct.starting_capital) if acct else Decimal(0)
    costs = running_costs(session, start, now)
    if sleeve:
        trading_net = sum((p.pnl or Decimal(0) for p in closed), Decimal(0))
        share = cfg.sleeves[sleeve].capital_fraction
        costs = type(costs)(
            **{**costs.__dict__, "total": costs.total * share}
        )  # its share of costs
    net = trading_net - costs.total
    peak, dd = Decimal(0), Decimal(0)
    for _, eq in series:
        peak = max(peak, eq)
        if peak > 0:
            dd = max(dd, (peak - eq) / peak)
    liqs = acct.liquidations if acct else 0
    bot_sharpe = sharpe(daily_returns(series), 365)
    btc_sharpe = sharpe(btc_daily_returns(session, cfg, start), 365)
    incident_since = now - timedelta(days=INCIDENT_WINDOW_DAYS)
    incidents = session.execute(
        select(func.count())
        .select_from(FetchRun)
        .where(FetchRun.started_at >= incident_since, FetchRun.ok.is_(False))
    ).scalar_one()
    state = session.get(RiskState, 1)
    selftest_ok = bool(state and state.live_selftest_at)
    criteria = [
        Criterion("shadow days ≥ 28", f"{days:.1f}", days >= MIN_SHADOW_DAYS),
        Criterion(
            "closed primary trades ≥ 100", str(len(closed)), len(closed) >= MIN_CLOSED_TRADES
        ),
        Criterion(
            "net positive after all costs (fees, interest, LLM, X, server)",
            f"{net:+.2f} = trading {trading_net:+.2f} - costs {costs.total:.2f}",
            net > 0,
        ),
        Criterion("profit factor ≥ 1.3", f"{pf:.2f}", pf >= MIN_PROFIT_FACTOR),
        Criterion(
            "sharpe above BTC buy-and-hold",
            f"{_fmt(bot_sharpe)} vs {_fmt(btc_sharpe)}",
            bot_sharpe is not None and (btc_sharpe is None or bot_sharpe > btc_sharpe),
        ),
        Criterion("max drawdown < 10%", f"{dd:.1%}", dd < MAX_DRAWDOWN),
        Criterion("zero simulated liquidations", str(liqs), liqs == 0),
        Criterion(
            "no operational incidents, last 2 weeks",
            f"{incidents} failed fetches",
            incidents < INCIDENT_ERRORS_IN_ROW,
        ),
        Criterion(
            "live self-test passed (order, borrow, close, kill)",
            "yes" if selftest_ok else "no",
            selftest_ok,
        ),
        Criterion(
            "executor price feed is the public WebSocket",
            cfg.exchange.price_feed,
            cfg.exchange.price_feed == "ws",
        ),
        Criterion(
            "live_allowed = true in config", str(cfg.trading.live_allowed), cfg.trading.live_allowed
        ),
    ]
    return Verdict(all(c.ok for c in criteria), criteria, now)


def decide(session: Session, cfg: Config, now: datetime | None = None) -> Verdict:
    """Flip to live when every criterion holds; record the verdict either way. With
    sleeves, each sleeve is judged on its own record and flips on its own; the process
    goes live when the first sleeve does (the others keep trading on paper)."""
    now = now or datetime.now(UTC)
    verdict = evaluate(session, cfg, now)
    state = session.get(RiskState, 1)
    if state is None:
        return verdict
    state.last_golive_check = now
    report: dict[str, Any] = {"book": [c.__dict__ for c in verdict.criteria]}
    any_ready = verdict.ready and not cfg.sleeves
    if cfg.sleeves:
        modes = dict(state.sleeve_modes or {})
        for name in cfg.sleeves:
            v = evaluate(session, cfg, now, sleeve=name)
            report[name] = [c.__dict__ for c in v.criteria]
            if v.ready and modes.get(name) != "live":
                modes[name] = "live"
                any_ready = True
            modes.setdefault(name, "shadow")
        state.sleeve_modes = modes
        any_ready = any_ready or any(m == "live" for m in modes.values())
    state.golive_report = report
    if any_ready and state.mode != "live":
        state.mode, state.live_since = "live", now
        state.leverage_ceiling = Decimal(2)
        state.capital_fraction = cfg.trading.live_start_fraction
        state.capital_step_at = now
        state.starting_capital = None  # the executor sets it from the first live equity
    session.commit()
    return verdict


# --- leverage ramp (§8) -------------------------------------------------------------------


def _live_net(session: Session, since: datetime, until: datetime) -> Decimal:
    """Live P&L of trades closed in the window, after all costs of that window."""
    trading = sum(
        (
            p.pnl or Decimal(0)
            for p in _closed(session, mode="live")
            if p.closed_at and since <= p.closed_at < until
        ),
        Decimal(0),
    )
    return trading - running_costs(session, since, until).total


def ramp(session: Session, now: datetime | None = None) -> Decimal:
    """2x at live start; 5x after 4 weeks within all limits; 10x after 3 months net-positive
    after all costs. Drops back a step on a drawdown pause or a losing month."""
    now = now or datetime.now(UTC)
    state = session.get(RiskState, 1)
    if state is None or state.mode != "live" or not state.live_since:
        return Decimal(2)
    weeks = (now - state.live_since).total_seconds() / (7 * 86400)
    month_ago = now - timedelta(days=30)
    net_month = _live_net(session, max(month_ago, state.live_since), now)
    net_all = _live_net(session, state.live_since, now)
    recent_pause = bool(state.last_pause_at and state.last_pause_at >= month_ago)
    ceiling = Decimal(2)
    if weeks >= 4 and not recent_pause and net_month >= 0:
        ceiling = Decimal(5)
    if weeks >= 13 and not recent_pause and net_all > 0 and net_month >= 0:
        ceiling = Decimal(10)
    state.leverage_ceiling = ceiling
    session.commit()
    return ceiling


# --- capital ramp (§10) -------------------------------------------------------------------


def capital_steps(start: Decimal) -> list[Decimal]:
    return [start, *(s for s in CAPITAL_STEPS if s > start)]


def next_capital_fraction(
    current: Decimal,
    start: Decimal,
    *,
    days_in_step: float,
    paused_in_step: bool,
    braked: bool,
    net_in_step: Decimal,
) -> Decimal:
    """The capital ramp as arithmetic. One step back after a drawdown pause (never below
    the start); one step up after ``CAPITAL_STEP_DAYS`` within limits and net positive
    after all costs; otherwise unchanged. A fraction off the ladder snaps to the step
    below it."""
    steps = capital_steps(start)
    index = max((i for i, s in enumerate(steps) if s <= current), default=0)
    if paused_in_step or braked:
        return steps[max(0, index - 1)] if paused_in_step else steps[index]
    if days_in_step >= CAPITAL_STEP_DAYS and net_in_step > 0:
        return steps[min(len(steps) - 1, index + 1)]
    return steps[index]


def capital_ramp(session: Session, cfg: Config, now: datetime | None = None) -> Decimal:
    """Move ``risk_state.capital_fraction`` along 10% → 25% → 50% → 100% of
    ``capital_max_usdt``. Every move restarts the three-week clock."""
    now = now or datetime.now(UTC)
    start = cfg.trading.live_start_fraction
    state = session.get(RiskState, 1)
    if state is None or state.mode != "live" or not state.live_since:
        return start
    current = state.capital_fraction or start
    step_at = state.capital_step_at or state.live_since
    new = next_capital_fraction(
        current,
        start,
        days_in_step=(now - step_at).total_seconds() / 86400,
        paused_in_step=bool(state.last_pause_at and state.last_pause_at > step_at),
        braked=bool(state.emergency_brake),
        net_in_step=_live_net(session, step_at, now),
    )
    paused = bool(state.last_pause_at and state.last_pause_at > step_at)
    if new != current or paused or state.capital_fraction is None:
        state.capital_fraction, state.capital_step_at = new, now
        session.commit()
    return new
