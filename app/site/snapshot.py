"""Public-site snapshot (SPEC §12 M8a): built from an explicit whitelist schema, never
from dumped rows. Every string passes the number whitelist in site mode. A test fails if
any forbidden field or an amount reaches the output.

    python -m app.site.snapshot            # writes site/public/data/snapshot.json
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.config import Config, get_config
from app.db.models import (
    AgentOutputRecord,
    Candle,
    Cycle,
    DecisionRecord,
    EquitySnapshot,
    Heartbeat,
    PaperAccount,
    Position,
    XPostOut,
)
from app.db.session import new_session
from app.social.templates import holding_text
from app.social.whitelist import check

OUT = Path(__file__).resolve().parents[2] / "site" / "public" / "data" / "snapshot.json"
FORBIDDEN_KEYS = {
    "notional",
    "margin",
    "qty",
    "quantity",
    "cash",
    "balance",
    "equity_usdt",
    "cost",
    "cost_usd",
    "api_key",
    "secret",
    "subaccount",
    "account_id",
    "order_link_id",
    "text",
    "author",
    "fees",
    "interest",
    "borrowed",
    "pnl",
    "size",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OpenTrade(_Strict):
    asset: str
    cashtag: str
    direction: str
    entry: float
    leverage: float
    stop: float
    target: float
    time_in_trade: str
    unrealized_price_pct: float
    unrealized_margin_pct: float
    paper: bool


class ClosedTrade(_Strict):
    asset: str
    cashtag: str
    direction: str
    entry: float
    exit: float
    leverage: float
    price_pct: float
    margin_pct: float
    holding: str
    opened_at: datetime
    closed_at: datetime
    x_url: str | None  # link to the X thread (allowed on the site, never in posts)
    paper: bool


class AgentScore(_Strict):
    agent: str
    score: float
    confidence: float
    valid: bool
    reasons: list[str] = Field(max_length=3)


class AssetView(_Strict):
    asset: str
    cashtag: str
    agents: list[AgentScore]
    pm_claude: str | None
    pm_gpt: str | None
    consensus: str
    consensus_score: float | None
    formula_score: float | None
    reason: str
    macro_regime: str | None
    coupling: float | None
    macro_tradfi: float | None
    macro_native: float | None


class Performance(_Strict):
    since: datetime
    bot_pct: float
    btc_hold_pct: float
    basket_pct: float
    drawdown_pct: float
    equity_curve: list[list[float]]  # [[unix_ms, pct], ...]
    btc_curve: list[list[float]] = Field(default_factory=list)  # BTC buy & hold, same units
    basket_curve: list[list[float]] = Field(default_factory=list)  # equal-weight basket


class AssetContribution(_Strict):
    asset: str
    cashtag: str
    trades: int
    wins: int
    pct: float  # realized contribution to equity, % of starting capital


class CycleRow(_Strict):
    ts: datetime
    scores: dict[str, float | None]  # consensus score per asset
    directions: dict[str, str]  # long | short | flat per asset


class Stats(_Strict):
    trades: int
    win_rate_pct: float | None
    profit_factor: float | None
    avg_holding: str | None


class Leader(_Strict):
    name: str
    kind: str  # agent | model
    ic_1d: float | None
    sample: int


class Snapshot(_Strict):
    generated_at: datetime
    mode: str  # shadow | live
    performance: Performance
    stats: Stats
    open_trades: list[OpenTrade]
    closed_trades: list[ClosedTrade]
    assets: list[AssetView]
    leaderboard: list[Leader]
    heartbeat_ok: bool
    handle: str
    by_asset: list[AssetContribution] = Field(default_factory=list)
    history: list[CycleRow] = Field(default_factory=list)  # last week of cycles, oldest first


def _f(x: Decimal | float | None, places: int = 4) -> float:
    return round(float(x or 0), places)


def _pct_curve(cfg: Config, s: Any, since: datetime) -> tuple[list[list[float]], float, float]:
    rows = s.execute(
        select(EquitySnapshot.ts, EquitySnapshot.equity)
        .where(EquitySnapshot.mode == "paper:primary", EquitySnapshot.ts >= since)
        .order_by(EquitySnapshot.ts)
    ).all()
    rows = [r for r in rows if float(r.equity) > 0]  # skip pre-funding zero snapshots
    if not rows:
        return [], 0.0, 0.0
    base = float(rows[0].equity)
    curve, peak, dd = [], -1e9, 0.0
    for i, r in enumerate(rows):
        pct = (float(r.equity) / base - 1) * 100
        peak = max(peak, pct)
        dd = min(dd, pct - peak)
        if i % max(1, len(rows) // 400) == 0 or i == len(rows) - 1:
            curve.append([r.ts.timestamp() * 1000, round(pct, 3)])
    return curve, round(curve[-1][1], 3), round(dd, 3)


def _benchmark_curves(
    cfg: Config, s: Any, since: datetime
) -> tuple[list[list[float]], list[list[float]]]:
    """BTC buy-and-hold and the equal-weight basket as % curves on the hourly candles,
    thinned to about 400 points like the equity curve."""
    per_asset: dict[str, dict[datetime, float]] = {}
    for asset in cfg.trading.assets:
        rows = s.execute(
            select(Candle.open_time, Candle.close)
            .where(
                Candle.symbol == cfg.symbol(asset),
                Candle.interval == "60",
                Candle.open_time >= since,
            )
            .order_by(Candle.open_time)
        ).all()
        if len(rows) >= 2:
            base = float(rows[0].close)
            per_asset[asset] = {r.open_time: (float(r.close) / base - 1) * 100 for r in rows}
    if not per_asset:
        return [], []
    times = sorted(set.intersection(*(set(d) for d in per_asset.values())))
    step = max(1, len(times) // 400)
    times = times[::step] + ([times[-1]] if times and (len(times) - 1) % step else [])
    btc = per_asset.get("BTC", {})
    btc_curve = [[ts.timestamp() * 1000, round(btc[ts], 3)] for ts in times if ts in btc]
    basket_curve = [
        [ts.timestamp() * 1000, round(sum(d[ts] for d in per_asset.values()) / len(per_asset), 3)]
        for ts in times
    ]
    return btc_curve, basket_curve


def _history(cfg: Config, s: Any, since: datetime) -> list[CycleRow]:
    rows = s.execute(
        select(
            DecisionRecord.cycle_id,
            DecisionRecord.ts,
            DecisionRecord.asset,
            DecisionRecord.consensus_score,
            DecisionRecord.direction,
        )
        .where(DecisionRecord.ts >= since)
        .order_by(DecisionRecord.cycle_id)
    ).all()
    by_cycle: dict[int, CycleRow] = {}
    for r in rows:
        row = by_cycle.get(r.cycle_id)
        if row is None:
            row = by_cycle[r.cycle_id] = CycleRow(
                ts=r.ts, scores={a: None for a in cfg.trading.assets}, directions={}
            )
        row.scores[r.asset] = _f(r.consensus_score, 3) if r.consensus_score is not None else None
        row.directions[r.asset] = r.direction
    return list(by_cycle.values())[-60:]


def _benchmarks(cfg: Config, s: Any, since: datetime) -> tuple[float, float]:
    rets = []
    for asset in cfg.trading.assets:
        rows = (
            s.execute(
                select(Candle.close)
                .where(
                    Candle.symbol == cfg.symbol(asset),
                    Candle.interval == "60",
                    Candle.open_time >= since,
                )
                .order_by(Candle.open_time)
            )
            .scalars()
            .all()
        )
        rets.append((float(rows[-1]) / float(rows[0]) - 1) * 100 if len(rows) >= 2 else 0.0)
    btc = rets[cfg.trading.assets.index("BTC")] if "BTC" in cfg.trading.assets else 0.0
    return round(btc, 3), round(sum(rets) / len(rets), 3) if rets else 0.0


def build(cfg: Config | None = None, now: datetime | None = None) -> Snapshot:
    cfg = cfg or get_config()
    now = now or datetime.now(UTC)
    tags = cfg.posting.cashtags
    with new_session() as s:
        acct = s.get(PaperAccount, 1)
        since = (acct.updated_at - timedelta(days=90)) if acct else now - timedelta(days=90)
        first = s.execute(
            select(func.min(EquitySnapshot.ts)).where(EquitySnapshot.mode == "paper:primary")
        ).scalar_one()
        since = first or since
        curve, bot_pct, dd = _pct_curve(cfg, s, since)
        btc_pct, basket_pct = _benchmarks(cfg, s, since)
        btc_curve, basket_curve = _benchmark_curves(cfg, s, since)
        history = _history(cfg, s, now - timedelta(days=7))
        closed = s.scalars(
            select(Position)
            .where(Position.track == "primary", Position.status == "closed")
            .order_by(Position.closed_at.desc())
            .limit(200)
        ).all()
        wins = [p for p in closed if p.pnl and p.pnl > 0]
        losses = [p for p in closed if p.pnl and p.pnl < 0]
        gross_win = sum(float(p.pnl or 0) for p in wins)
        gross_loss = -sum(float(p.pnl or 0) for p in losses)
        holds = [
            (p.closed_at - p.opened_at).total_seconds() / 3600
            for p in closed
            if p.closed_at and p.opened_at
        ]
        stats = Stats(
            trades=len(closed),
            win_rate_pct=round(len(wins) / len(closed) * 100, 1) if closed else None,
            profit_factor=round(gross_win / gross_loss, 2) if gross_loss else None,
            avg_holding=holding_text(sum(holds) / len(holds)) if holds else None,
        )
        posted = {
            r.position_id: r
            for r in s.scalars(
                select(XPostOut).where(
                    XPostOut.x_id.is_not(None),  # a blocked post is "posted" but has no id
                    XPostOut.kind == "open",
                )
            ).all()
        }
        open_rows = s.scalars(
            select(Position).where(
                Position.track == "primary", Position.status.in_(["open", "closing"])
            )
        ).all()
        open_trades = []
        for p in open_rows:
            post = posted.get(p.id)
            if post is None or post.dry_run:
                continue  # a trade appears only after its X post (SPEC §12)
            spot = s.execute(
                select(Candle.close)
                .where(Candle.symbol == p.symbol, Candle.interval == "15")
                .order_by(Candle.open_time.desc())
                .limit(1)
            ).scalar_one_or_none()
            price_pct = (
                ((float(spot) / float(p.entry_price) - 1) * (1 if p.direction == "long" else -1))
                if spot and p.entry_price
                else 0.0
            )
            open_trades.append(
                OpenTrade(
                    asset=p.asset,
                    cashtag=tags.get(p.asset, f"${p.asset}"),
                    direction=p.direction,
                    entry=_f(p.entry_price),
                    leverage=_f(p.leverage, 1),
                    stop=_f(p.stop),
                    target=_f(p.target),
                    time_in_trade=holding_text((now - p.opened_at).total_seconds() / 3600)
                    if p.opened_at
                    else "",
                    unrealized_price_pct=round(price_pct * 100, 2),
                    unrealized_margin_pct=round(price_pct * float(p.leverage) * 100, 2),
                    paper=p.mode == "paper",
                )
            )
        base_capital = float(acct.starting_capital) if acct and acct.starting_capital else 0.0
        by_asset = []
        for asset in cfg.trading.assets:
            mine_closed = [p for p in closed if p.asset == asset]
            by_asset.append(
                AssetContribution(
                    asset=asset,
                    cashtag=tags.get(asset, f"${asset}"),
                    trades=len(mine_closed),
                    wins=sum(1 for p in mine_closed if p.pnl and p.pnl > 0),
                    pct=round(sum(float(p.pnl or 0) for p in mine_closed) / base_capital * 100, 3)
                    if base_capital
                    else 0.0,
                )
            )
        closed_trades = [
            ClosedTrade(
                asset=p.asset,
                cashtag=tags.get(p.asset, f"${p.asset}"),
                direction=p.direction,
                entry=_f(p.entry_price),
                exit=_f(p.exit_price),
                leverage=_f(p.leverage, 1),
                price_pct=_f((p.pnl_price_pct or 0) * 100, 2),
                margin_pct=_f((p.pnl_margin_pct or 0) * 100, 2),
                holding=holding_text((p.closed_at - p.opened_at).total_seconds() / 3600)
                if p.closed_at and p.opened_at
                else "",
                opened_at=p.opened_at or p.closed_at or now,
                closed_at=p.closed_at or now,
                x_url=(
                    f"https://x.com/{cfg.posting.handle}/status/{posted[p.id].x_id}"
                    if p.id in posted and not posted[p.id].dry_run
                    else None
                ),
                paper=p.mode == "paper",
            )
            for p in closed  # every closed primary trade; the thread link only when posted
        ]
        last_cycle = s.execute(
            select(Cycle).where(Cycle.status == "done").order_by(Cycle.id.desc()).limit(1)
        ).scalar_one_or_none()
        assets: list[AssetView] = []
        if last_cycle:
            outs = s.scalars(
                select(AgentOutputRecord).where(AgentOutputRecord.cycle_id == last_cycle.id)
            ).all()
            decs = {
                d.asset: d
                for d in s.scalars(
                    select(DecisionRecord).where(DecisionRecord.cycle_id == last_cycle.id)
                ).all()
            }
            for asset in cfg.trading.assets:
                d = decs.get(asset)
                mine = [o for o in outs if o.asset == asset]
                macro = next((o for o in mine if o.agent == "macro"), None)
                comp = (macro.components or {}) if macro else {}
                regime = macro.evidence[0].split(" ")[1] if macro and macro.evidence else None
                coupling = None
                if macro and macro.evidence:
                    try:
                        coupling = float(macro.evidence[0].split("coupling ")[1])
                    except (IndexError, ValueError):
                        coupling = None
                reasons = (
                    [
                        str(r)
                        for r in ((d.proposal or {}).get("reasons") or [])
                        if check(str(r), site=True).ok
                    ][:3]
                    if d
                    else []
                )
                assets.append(
                    AssetView(
                        asset=asset,
                        cashtag=tags.get(asset, f"${asset}"),
                        agents=[
                            AgentScore(
                                agent=o.agent,
                                score=_f(o.score, 3),
                                confidence=_f(o.confidence, 3),
                                valid=o.valid,
                                reasons=[
                                    str(e)[:160]
                                    for e in o.evidence[:3]
                                    if check(str(e), site=True).ok
                                ][:2],
                            )
                            for o in mine
                        ],
                        pm_claude=d.pm1_direction if d else None,
                        pm_gpt=d.pm2_direction if d else None,
                        consensus=d.direction if d else "flat",
                        consensus_score=_f(d.consensus_score, 3)
                        if d and d.consensus_score is not None
                        else None,
                        formula_score=_f(d.formula_score, 3)
                        if d and d.formula_score is not None
                        else None,
                        reason=(d.reason if d else "no cycle yet")[:120]
                        + (": " + reasons[0][:140] if reasons else ""),
                        macro_regime=regime,
                        coupling=coupling,
                        macro_tradfi=comp.get("tradfi"),
                        macro_native=comp.get("native"),
                    )
                )
        hb = s.execute(select(func.max(Heartbeat.ts))).scalar_one()
        from app.decision.tuning import leaderboard as _leaderboard

        leaders = [
            Leader(
                name=r.name[:60],
                kind=r.kind,
                ic_1d=_f(r.ic.get("1d"), 3) if r.ic.get("1d") is not None else None,
                sample=r.sample,
            )
            for r in _leaderboard(s, cfg, since)
        ]
    return Snapshot(
        generated_at=now,
        mode="live" if cfg.trading.live_allowed else "shadow",
        performance=Performance(
            since=since,
            bot_pct=bot_pct,
            btc_hold_pct=btc_pct,
            basket_pct=basket_pct,
            drawdown_pct=dd,
            equity_curve=curve,
            btc_curve=btc_curve,
            basket_curve=basket_curve,
        ),
        by_asset=by_asset,
        history=history,
        stats=stats,
        open_trades=open_trades,
        closed_trades=closed_trades,
        assets=assets,
        leaderboard=leaders,
        heartbeat_ok=bool(hb and now - hb < timedelta(minutes=5)),
        handle=cfg.posting.handle,
    )


def verify(snapshot: Snapshot) -> list[str]:
    """Forbidden keys anywhere, or a string failing the site whitelist, are problems."""
    return verify_dict(json.loads(snapshot.model_dump_json()))


def verify_dict(data: dict[str, Any]) -> list[str]:
    problems: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in FORBIDDEN_KEYS:
                    problems.append(f"forbidden key {path}.{k}")
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and not path.endswith(
            (
                ".x_url",
                ".generated_at",
                ".since",
                ".opened_at",
                ".closed_at",
                ".handle",
                ".name",
                ".ts",
            )
        ):
            r = check(node, site=True)
            if not r.ok:
                problems.append(f"{path}: {r.problems}")

    walk(data, "$")
    return problems


def main() -> int:
    snap = build()
    problems = verify(snap)
    if problems:
        print("\n".join(problems))
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(snap.model_dump_json(indent=1))
    print(
        f"wrote {OUT} ({OUT.stat().st_size} bytes), {len(snap.assets)} assets, "
        f"{len(snap.closed_trades)} closed trades"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
