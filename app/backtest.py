"""Backtest harness for the indicators agent (SPEC §10: only code-based agents are backtestable).

For each asset the agent score is computed at every closed bar from the bars before it,
then compared with forward returns over 4 hours, 1 day and 3 days:

- IC: Spearman rank correlation between score and forward return
- hit rate: share of bars where sign(score) == sign(forward return), scores near zero skipped
- long-short spread: mean forward return of the top-quintile scores minus the bottom quintile

    python -m app.backtest [--interval 240] [--days 180]

The harness never trades; the risk engine's rules (M5) are tested on their own.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.agents.indicators import BARS_PER_DAY, features, score_series
from app.config import Config, get_config
from app.db.models import Candle
from app.db.session import new_session

HORIZONS_HOURS = {"4h": 4, "1d": 24, "3d": 72}
DEAD_ZONE = 0.1  # |score| below this is "no view" for the hit rate


@dataclass(frozen=True)
class HorizonStats:
    horizon: str
    n: int
    ic: float
    hit_rate: float
    spread: float  # top minus bottom quintile mean forward return


@dataclass(frozen=True)
class AssetResult:
    asset: str
    bars: int
    stats: list[HorizonStats]


def forward_returns(close: pd.Series, bars: int) -> pd.Series:
    return close.shift(-bars) / close - 1


def evaluate_scores(score: pd.Series, close: pd.Series, interval: str) -> list[HorizonStats]:
    per_day = BARS_PER_DAY[interval]
    out: list[HorizonStats] = []
    for name, hours in HORIZONS_HOURS.items():
        bars = max(1, round(hours * per_day / 24))
        fwd = forward_returns(close, bars)
        df = pd.DataFrame({"score": score, "fwd": fwd}).dropna()
        if len(df) < 30:
            out.append(HorizonStats(name, len(df), float("nan"), float("nan"), float("nan")))
            continue
        ic = float(df["score"].rank().corr(df["fwd"].rank()))  # Spearman without scipy
        active = df[df["score"].abs() >= DEAD_ZONE]
        hit = (
            float((np.sign(active["score"]) == np.sign(active["fwd"])).mean())
            if len(active)
            else float("nan")
        )
        q_hi, q_lo = df["score"].quantile(0.8), df["score"].quantile(0.2)
        spread = float(
            df.loc[df["score"] >= q_hi, "fwd"].mean() - df.loc[df["score"] <= q_lo, "fwd"].mean()
        )
        out.append(HorizonStats(name, len(df), ic, hit, spread))
    return out


def backtest_frame(asset: str, candles: pd.DataFrame, interval: str) -> AssetResult:
    df = features(candles, interval)
    df.attrs["interval"] = interval
    score = score_series(df)
    return AssetResult(
        asset, int(score.notna().sum()), evaluate_scores(score, df["close"], interval)
    )


def load(cfg: Config, asset: str, interval: str, days: int) -> pd.DataFrame:
    since = datetime.now(UTC) - timedelta(days=days)
    with new_session() as session:
        rows = session.execute(
            select(
                Candle.open_time, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume
            )
            .where(
                Candle.symbol == cfg.symbol(asset),
                Candle.interval == interval,
                Candle.open_time >= since,
            )
            .order_by(Candle.open_time)
        ).all()
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    return df.set_index("open_time").astype(float)


def render(results: list[AssetResult]) -> str:
    lines = [f"{'asset':8s} {'bars':>5s}  " + "  ".join(f"{h:>22s}" for h in HORIZONS_HOURS)]
    lines.append(
        " " * 16 + "  ".join(f"{'IC':>6s} {'hit':>6s} {'spread':>8s}" for _ in HORIZONS_HOURS)
    )
    for r in results:
        cells = [f"{s.ic:+6.2f} {s.hit_rate:6.1%} {s.spread:+8.2%}" for s in r.stats]
        lines.append(f"{r.asset:8s} {r.bars:5d}  " + "  ".join(cells))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--interval", default="240", choices=["60", "240", "D"])
    parser.add_argument("--days", type=int, default=180)
    args = parser.parse_args(argv)
    cfg = get_config()
    results = []
    for asset in cfg.trading.assets:
        candles = load(cfg, asset, args.interval, args.days)
        if len(candles) < 250:
            print(f"{asset}: only {len(candles)} bars stored; fetch more history first")
            continue
        results.append(backtest_frame(asset, candles, args.interval))
    print(
        f"indicators backtest, interval {args.interval}, last {args.days} days, "
        "scores from bar 210 on\n"
    )
    print(render(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
