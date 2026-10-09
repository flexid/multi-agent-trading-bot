"""Go-live checker and leverage ramp (SPEC §10, §8; owner rules 2026-10-09).

The bot decides itself. ``evaluate`` returns every criterion with its value and pass/fail
on the primary paper track; ``decide`` flips ``risk_state.mode`` to live only when all
pass AND ``trading.live_allowed`` is true AND the live self-test has passed. The ramp
moves ``risk_state.leverage_ceiling`` 2x → 5x → 10x and back down on underperformance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config
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


def _closed(session: Session, mode: str = "paper") -> list[Position]:
    return list(
        session.scalars(
            select(Position).where(
                Position.track == "primary", Position.status == "closed", Position.mode == mode
            )
        ).all()
    )


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


def evaluate(session: Session, cfg: Config, now: datetime | None = None) -> Verdict:
    now = now or datetime.now(UTC)
    series = _equity_series(session)
    start = series[0][0] if series else now
    days = (now - start).total_seconds() / 86400
    closed = _closed(session)
    wins = sum(float(p.pnl) for p in closed if p.pnl and p.pnl > 0)
    losses = -sum(float(p.pnl) for p in closed if p.pnl and p.pnl < 0)
    pf = Decimal(str(wins / losses)) if losses > 0 else (Decimal(99) if wins > 0 else Decimal(0))
    acct = session.get(PaperAccount, 1)
    net = (acct.equity - acct.starting_capital) if acct else Decimal(0)
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
        Criterion("net positive after fees and interest", f"{net:+.2f}", net > 0),
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
            "live_allowed = true in config", str(cfg.trading.live_allowed), cfg.trading.live_allowed
        ),
    ]
    return Verdict(all(c.ok for c in criteria), criteria, now)


def decide(session: Session, cfg: Config, now: datetime | None = None) -> Verdict:
    """Flip to live when every criterion holds; record the verdict either way."""
    now = now or datetime.now(UTC)
    verdict = evaluate(session, cfg, now)
    state = session.get(RiskState, 1)
    if state is None:
        return verdict
    state.last_golive_check = now
    state.golive_report = [c.__dict__ for c in verdict.criteria]
    if verdict.ready and state.mode != "live":
        state.mode, state.live_since = "live", now
        state.leverage_ceiling = Decimal(2)
        state.starting_capital = None  # the executor sets it from the first live equity
    session.commit()
    return verdict


# --- leverage ramp (§8) -------------------------------------------------------------------


def ramp(session: Session, now: datetime | None = None) -> Decimal:
    """2x at live start; 5x after 4 weeks within all limits; 10x after 3 months net-positive
    after costs. Drops back a step on a drawdown pause or a losing month."""
    now = now or datetime.now(UTC)
    state = session.get(RiskState, 1)
    if state is None or state.mode != "live" or not state.live_since:
        return Decimal(2)
    weeks = (now - state.live_since).total_seconds() / (7 * 86400)
    closed = _closed(session, mode="live")
    month_ago = now - timedelta(days=30)
    net_month = sum(float(p.pnl or 0) for p in closed if p.closed_at and p.closed_at >= month_ago)
    net_all = sum(float(p.pnl or 0) for p in closed)
    recent_pause = bool(state.last_pause_at and state.last_pause_at >= month_ago)
    ceiling = Decimal(2)
    if weeks >= 4 and not recent_pause and net_month >= 0:
        ceiling = Decimal(5)
    if weeks >= 13 and not recent_pause and net_all > 0 and net_month >= 0:
        ceiling = Decimal(10)
    state.leverage_ceiling = ceiling
    session.commit()
    return ceiling
