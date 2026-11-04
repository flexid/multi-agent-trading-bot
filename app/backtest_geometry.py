# ruff: noqa: E501
"""Replay every primary trade since the record line over its 15-minute candles under
alternative exit geometries (owner 2026-11-04: "no way to act faster?"). The entries are
the ones the bot took; only the stop placement, the leverage, the take and a dead-trade
rule change. The same two weeks of market, so evidence, not proof.

python -m app.backtest_geometry
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text

from app.db.models import Candle
from app.db.session import new_session

FEE = Decimal("0.001")  # per leg, of notional
TAKE = Decimal("0.02")  # net on margin
SINCE = "2026-10-12 19:33+00"


@dataclass(frozen=True)
class Variant:
    name: str
    room: Decimal  # the managers' stop distance × this
    hard_atr: Decimal  # the hard stop this many ATR beyond the soft one
    lev_max: Decimal  # ceiling; the 4% cap decides below it
    keep: Decimal | None  # v14 floor: level + keep × gain above; None = v10 giveback 0.25
    dead_hours: int | None  # close when still under water after this long
    skip_follow_atr: Decimal | None  # skip a trade following its own 24h move beyond this
    skip_with_leader: bool = False  # skip a short after BTC fell >1% in 24h, a long after it rose


VARIANTS = [
    Variant(
        "v14 as run (stops 1.0, hard 0.5 ATR, cap 3x, keep 0.5)",
        Decimal(1),
        Decimal("0.5"),
        Decimal(3),
        Decimal("0.5"),
        None,
        None,
    ),
    Variant(
        "v10 geometry (stops 1.5, hard 1 ATR, 1x, giveback 0.25)",
        Decimal("1.5"),
        Decimal(1),
        Decimal(1),
        None,
        None,
        None,
    ),
    Variant(
        "v10 geometry + dead trade at 24h", Decimal("1.5"), Decimal(1), Decimal(1), None, 24, None
    ),
    Variant(
        "v10 geometry + dead trade at 12h", Decimal("1.5"), Decimal(1), Decimal(1), None, 12, None
    ),
    Variant(
        "v14 + dead trade at 24h", Decimal(1), Decimal("0.5"), Decimal(3), Decimal("0.5"), 24, None
    ),
    Variant(
        "v10 geometry + skip trades following > 1 ATR",
        Decimal("1.5"),
        Decimal(1),
        Decimal(1),
        None,
        None,
        Decimal(1),
    ),
    Variant(
        "v10 geometry + dead 24h + skip following > 1 ATR",
        Decimal("1.5"),
        Decimal(1),
        Decimal(1),
        None,
        24,
        Decimal(1),
    ),
    Variant(
        "v14 + skip following > 1 ATR",
        Decimal(1),
        Decimal("0.5"),
        Decimal(3),
        Decimal("0.5"),
        None,
        Decimal(1),
    ),
    Variant(
        "v14 + skip following > 0.5 ATR",
        Decimal(1),
        Decimal("0.5"),
        Decimal(3),
        Decimal("0.5"),
        None,
        Decimal("0.5"),
    ),
    Variant(
        "v14 + skip a call that continues BTC's own >1% day",
        Decimal(1),
        Decimal("0.5"),
        Decimal(3),
        Decimal("0.5"),
        None,
        None,
        True,
    ),
    Variant(
        "v14 + both skips",
        Decimal(1),
        Decimal("0.5"),
        Decimal(3),
        Decimal("0.5"),
        None,
        Decimal(1),
        True,
    ),
    Variant(
        "v10 geometry + both skips",
        Decimal("1.5"),
        Decimal(1),
        Decimal(1),
        None,
        None,
        Decimal(1),
        True,
    ),
    Variant(
        "v10 geometry + both skips + dead 24h",
        Decimal("1.5"),
        Decimal(1),
        Decimal(1),
        None,
        24,
        Decimal(1),
        True,
    ),
]

TRADES_SQL = text(  # noqa: E501
    """
    select p.id, p.asset, p.direction, p.entry_price, p.atr, p.margin, p.opened_at, p.pnl, p.close_reason,
      (d.proposal->>'stop')::numeric pm_stop, (d.proposal->>'target')::numeric pm_target,
      (select (a->>'move_24h_atr')::numeric from jsonb_array_elements(c.evidence_pack->'assets') a where a->>'asset'=p.asset) own24,
      (select (a->>'change_24h_pct')::numeric from jsonb_array_elements(c.evidence_pack->'assets') a where a->>'asset'='BTC') btc24
    from positions p join decisions d on d.id=p.decision_id join cycles c on c.id=p.cycle_id
    where p.mode='paper' and p.track='primary' and p.status='closed' and p.opened_at >= :since
    order by p.opened_at
    """
)


def replay(t: Any, v: Variant, candles: list[Candle]) -> tuple[Decimal, str, float] | None:
    """(net pnl in quote, exit reason, hours) for one trade under a variant; None = skipped."""
    entry, atr, margin = Decimal(t.entry_price), Decimal(t.atr or 0), Decimal(t.margin)
    long = t.direction == "long"
    sign = 1 if long else -1
    if (
        v.skip_follow_atr is not None
        and t.own24 is not None
        and Decimal(t.own24) * sign > v.skip_follow_atr
    ):
        return None
    if v.skip_with_leader and t.btc24 is not None and Decimal(t.btc24) * sign < -1:
        return None
    planned = abs(Decimal(t.pm_stop) - entry) if t.pm_stop else atr * Decimal("1.5")
    soft_d = planned * v.room
    hard_d = soft_d + atr * v.hard_atr
    soft_pct = soft_d / entry
    cap = (Decimal("0.04") / soft_pct) if soft_pct else Decimal(1)
    lev = max(Decimal(1), min(v.lev_max, cap.quantize(Decimal("0.01"))))
    soft = entry - sign * soft_d
    hard = entry - sign * hard_d
    target = Decimal(t.pm_target) if t.pm_target else entry + sign * 2 * soft_d
    fees = 2 * FEE * lev  # both legs, as a share of margin
    peak: Decimal | None = None
    best = entry
    trail: Decimal | None = None
    start = t.opened_at
    for c in candles:
        if c.open_time < start:
            continue
        hours = (c.open_time - start).total_seconds() / 3600
        hi, lo, close = Decimal(c.high), Decimal(c.low), Decimal(c.close)
        worst = lo if long else hi
        bestp = hi if long else lo

        def net_at(px: Decimal) -> Decimal:
            return (px - entry) / entry * sign * lev - fees

        # touch exits inside the candle, the stop first when both touched
        if (long and worst <= hard) or (not long and worst >= hard):
            return net_at(hard) * margin, "stop_hard", hours
        if trail is not None and ((long and worst <= trail) or (not long and worst >= trail)):
            return net_at(trail) * margin, "trail", hours
        if (long and bestp >= target) or (not long and bestp <= target):
            return net_at(target) * margin, "target", hours
        # the close-based exits
        if (long and close <= soft) or (not long and close >= soft):
            return net_at(close) * margin, "stop", hours
        ret = net_at(close)
        if ret >= TAKE or peak is not None:
            peak = max(peak or Decimal(0), ret)
            if v.keep is not None:
                floor = max(TAKE, TAKE + (peak - TAKE) * v.keep)
            else:
                floor = max(TAKE, peak * Decimal("0.75"))
            if ret <= floor and peak > ret:
                return ret * margin, "take_profit", hours
        if v.dead_hours is not None and hours >= v.dead_hours and ret < 0:
            return ret * margin, "dead", hours
        # the 1R trailing stop, armed after +1R
        best = max(best, bestp) if long else min(best, bestp)
        if abs(best - entry) >= soft_d:
            cand = best - sign * soft_d
            trail = cand if trail is None else (max(trail, cand) if long else min(trail, cand))
        if hours >= 48:
            return ret * margin, "time", hours
    return None


def main() -> int:
    with new_session() as s:
        trades = s.execute(TRADES_SQL, {"since": SINCE}).all()
        by_symbol: dict[str, list[Candle]] = {}
        for t in trades:
            sym = f"{t.asset}USDT" if t.asset != "SPX6900" else "SPXUSDT"
            if sym not in by_symbol:
                by_symbol[sym] = list(
                    s.scalars(
                        select(Candle)
                        .where(
                            Candle.symbol == sym,
                            Candle.interval == "15",
                            Candle.open_time >= datetime.fromisoformat(SINCE),
                        )
                        .order_by(Candle.open_time)
                    ).all()
                )
        actual = sum(Decimal(t.pnl or 0) for t in trades)
        print(f"{len(trades)} trades since the record line; as booked: {actual:+.0f}\n")
        print(
            f"{'variant':62} {'n':>3} {'won':>4} {'pnl':>7} {'PF':>5} {'stops':>5} {'takes':>5} {'dead':>4} {'time':>4} {'hold h':>6}"
        )
        for v in VARIANTS:
            rows = [
                (
                    t,
                    replay(
                        t, v, by_symbol[f"{t.asset}USDT" if t.asset != "SPX6900" else "SPXUSDT"]
                    ),
                )
                for t in trades
            ]
            done = [(t, r) for t, r in rows if r is not None]
            if not done:
                continue
            pnls = [r[0] for _, r in done]
            wins = [x for x in pnls if x > 0]
            losses = [-x for x in pnls if x < 0]
            pf = (sum(wins) / sum(losses)) if losses else Decimal(99)
            reasons = [r[1] for _, r in done]
            hold = sum(r[2] for _, r in done) / len(done)
            print(
                f"{v.name:62} {len(done):3} {len(wins):4} {sum(pnls):+7.0f} {pf:5.2f} "
                f"{sum(1 for x in reasons if x.startswith('stop')):5} {sum(1 for x in reasons if x in ('take_profit', 'target', 'trail')):5} "
                f"{reasons.count('dead'):4} {reasons.count('time'):4} {hold:6.1f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
