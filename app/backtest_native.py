"""Backtest of the crypto-native macro inputs on daily bars (owner briefing 2026-10-09).

- Fear & Greed: the previous day's index through ``fng_score`` against each asset's
  forward returns (contrarian at extremes, near-neutral between).
- Dominance proxy: the 7-day change of ETHBTC, inverted and scaled as in the agent,
  through ``dominance_tilt`` per asset. CoinGecko dominance only accumulates from today,
  so the proxy is what can be tested.

    python -m app.backtest_native
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import select

from app.agents.macro import dominance_tilt, fng_score
from app.backtest import AssetResult, evaluate_scores, render
from app.config import Config, get_config
from app.db.models import Candle, MacroObservation
from app.db.session import new_session


def daily_closes(cfg: Config, symbol: str, days: int) -> pd.Series:
    with new_session() as session:
        rows = session.execute(
            select(Candle.open_time, Candle.close)
            .where(
                Candle.symbol == symbol,
                Candle.interval == "D",
                Candle.open_time >= datetime.now(UTC) - timedelta(days=days),
            )
            .order_by(Candle.open_time)
        ).all()
    s = pd.Series(
        [float(r.close) for r in rows], index=pd.DatetimeIndex([r.open_time for r in rows])
    )
    return s


def fng_series() -> pd.Series:
    with new_session() as session:
        rows = session.execute(
            select(MacroObservation.date, MacroObservation.value)
            .where(MacroObservation.series == "FNG")
            .order_by(MacroObservation.date)
        ).all()
    return pd.Series([float(r.value) for r in rows], index=pd.DatetimeIndex([r.date for r in rows]))


def main() -> int:
    cfg = get_config()
    days = 1000
    fng = fng_series()
    ethbtc = daily_closes(cfg, "ETHBTC", days)
    dom_change_pp = -(ethbtc / ethbtc.shift(7) - 1) * 100 * 0.6  # same proxy as the agent

    fng_results: list[AssetResult] = []
    dom_results: list[AssetResult] = []
    for asset in cfg.trading.assets:
        close = daily_closes(cfg, cfg.symbol(asset), days)
        if len(close) < 250:
            continue
        # Fear & Greed: index of the previous day, scored
        f = fng.reindex(close.index, method="ffill").shift(1)
        f_score = f.map(fng_score)
        fng_results.append(
            AssetResult(asset, int(f_score.notna().sum()), evaluate_scores(f_score, close, "D"))
        )
        # Dominance proxy: known at the bar's open from the previous day's close
        d = dom_change_pp.reindex(close.index, method="ffill").shift(1)
        d_score = d.map(lambda x, a=asset: dominance_tilt(a, x) if pd.notna(x) else float("nan"))
        dom_results.append(
            AssetResult(asset, int(d_score.notna().sum()), evaluate_scores(d_score, close, "D"))
        )

    print(f"daily bars, last {days} days (as available); 4h and 1d both mean 1 bar here\n")
    print("Fear & Greed (contrarian at extremes):")
    print(render(fng_results))
    print("\nBTC dominance proxy (ETHBTC 7d change, per-asset tilt):")
    print(render(dom_results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
