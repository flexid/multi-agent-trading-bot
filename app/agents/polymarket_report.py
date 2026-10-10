"""What Orak actually used (owner 2026-10-10): per asset, the markets that made it into
the last cycle's ladder and up/down reads, grouped by type, with count, 24 h volume and
each type's contribution to the score, plus what was fetched but excluded and why.

    python -m app.agents.polymarket_report [ASSET ...]
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime

from sqlalchemy import select

from app.agents.polymarket import (
    MAX_DAYS_TO_RESOLUTION,
    MIN_HOURS_TO_RESOLUTION,
    MIN_VOLUME_24H,
    Coverage,
    LadderPoint,
    UpDownPoint,
    _points,
    _price_at,
    score_ladder,
    score_updown,
)
from app.config import get_config
from app.db.models import Cycle, PolymarketMarket
from app.db.session import new_session

PRICE_OF = re.compile(r"^will the price of", re.I)
TOUCH = re.compile(r"^will \w+ (reach|hit|dip to|fall to|drop to)", re.I)


def market_type(m: PolymarketMarket, now: datetime) -> str:
    """A readable type from the mapping, the question's template and the time left."""
    mp = m.mapping or {}
    kind = mp.get("kind")
    if m.asset == "SP500":
        return "macro (S&P 500)"
    if kind == "updown":
        w = int(mp.get("window_min") or 0)
        if w < 60:
            return "up/down 5-15 min"
        label = "1h" if w == 60 else "4h" if w == 240 else "daily" if w >= 1440 else f"{w}min"
        return f"up/down {label}"
    if kind == "range":
        return "range buckets (price on a date)"
    if kind != "price" or m.end_date is None:
        return "other"
    days = (m.end_date - now).total_seconds() / 86400
    if PRICE_OF.match(m.question):
        return (
            "daily close ladder (above/below on a date)"
            if days <= 2
            else "close ladder, later date"
        )
    if TOUCH.match(m.question):
        if days <= 2:
            return "daily touch ladder (reach/dip on a date)"
        if days <= 10:
            return "weekly touch ladder"
        if days <= 45:
            return "monthly touch ladder"
        return "yearly touch ladder"
    return "threshold, other phrasing"


def report(assets: list[str] | None = None) -> str:
    cfg = get_config()
    assets = assets or cfg.trading.assets
    lines: list[str] = []
    with new_session() as s:
        last = s.execute(
            select(Cycle).where(Cycle.status == "done").order_by(Cycle.id.desc()).limit(1)
        ).scalar_one_or_none()
        now = last.started_at if last else datetime.now(UTC)
        lines.append(
            f"Orak's inputs as of cycle #{last.id if last else '-'} ({now:%Y-%m-%d %H:%M} UTC)"
        )
        for asset in assets:
            cov = Coverage(asset)
            ladder, updown = _points(s, asset, now, cov)
            by_id = {
                m.id: m
                for m in s.scalars(
                    select(PolymarketMarket).where(
                        PolymarketMarket.asset == asset, PolymarketMarket.closed.is_(False)
                    )
                )
            }
            score, level, shift, _ = score_ladder(ladder, 1.0) if ladder else (0.0, 0.0, 0.0, [])
            ud_score, _ = score_updown(updown)
            lines.append(
                f"\n== {asset}: {len(ladder)} ladder points, {len(updown)} up/down used; "
                f"level {level:+.2f}, 24h shift {shift:+.2f}, up/down {ud_score:+.2f}"
            )
            # used, by type
            lad_groups: dict[str, list[LadderPoint]] = defaultdict(list)
            ud_groups: dict[str, list[UpDownPoint]] = defaultdict(list)
            for p in ladder:
                lad_groups[market_type(by_id[p.market_id], now)].append(p)
            for u in updown:
                ud_groups[market_type(by_id[u.market_id], now)].append(u)
            total_w = sum(max(p.volume_24h, 1.0) for p in ladder) or 1.0
            lines.append(
                f"  {'used type':44s} {'n':>3s} {'24h volume':>12s} {'weight':>7s}  contribution"
            )
            for typ, pts in sorted(
                lad_groups.items(), key=lambda kv: -sum(p.volume_24h for p in kv[1])
            ):
                vol = sum(p.volume_24h for p in pts)
                _, lvl, sh, _ = score_ladder(pts, 1.0)
                w = f"{sum(max(p.volume_24h, 1.0) for p in pts) / total_w:6.0%}"
                contrib = (
                    f"level {lvl:+.2f}, shift {sh:+.2f} on its own; "
                    f"{w} of the ladder's volume weight"
                )
                lines.append(f"  {typ:44s} {len(pts):3d} {vol:12,.0f} {w:>7s}  {contrib}")
            for typ, uds in sorted(
                ud_groups.items(), key=lambda kv: -sum(u.volume_24h for u in kv[1])
            ):
                vol = sum(u.volume_24h for u in uds)
                sc, _ = score_updown(uds)
                contrib = f"up/down read {sc:+.2f} (0.25 of the score when present)"
                lines.append(f"  {typ:44s} {len(uds):3d} {vol:12,.0f} {'':>7s}  {contrib}")
            # excluded, by type and reason
            excl: Counter[tuple[str, str]] = Counter()
            used_ids = {p.market_id for p in ladder} | {p.market_id for p in updown}
            for m in by_id.values():
                if m.id in used_ids:
                    continue
                mp = m.mapping or {}
                kind = mp.get("kind")
                typ = market_type(m, now)
                if kind in ("range", "other"):
                    why = "not a single-threshold market"
                elif kind == "updown" and int(mp.get("window_min") or 0) < 60:
                    why = "window under an hour (noise)"
                elif m.end_date is None:
                    why = "no end date"
                else:
                    hours = (m.end_date - now).total_seconds() / 3600
                    latest = _price_at(s, m.id, now)
                    if hours < MIN_HOURS_TO_RESOLUTION:
                        why = "resolves within the hour"
                    elif kind == "price" and hours > MAX_DAYS_TO_RESOLUTION * 24:
                        why = f"resolves beyond {MAX_DAYS_TO_RESOLUTION} days"
                    elif latest is None:
                        why = "no price stored yet"
                    elif latest[1] < MIN_VOLUME_24H:
                        why = f"24h volume under ${MIN_VOLUME_24H:,.0f}"
                    elif hours <= 0:
                        why = "already resolved"
                    else:
                        why = "other"
                excl[(typ, why)] += 1
            if excl:
                lines.append("  excluded:")
                for (typ, why), n in sorted(excl.items(), key=lambda kv: -kv[1]):
                    lines.append(f"    {n:3d}  {typ:44s} {why}")
            if cov.unmapped:
                lines.append(f"    {cov.unmapped:3d}  (unmapped yet)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    print(report(args or None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
