"""Load candles and perp metrics from Postgres and run the indicators agent for each asset.

python -m app.agents.run_indicators [--interval 240]
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.indicators import PerpInputs, candles_frame, evaluate
from app.agents.schema import AgentOutput
from app.config import Config, get_config
from app.data.fetch import data_age
from app.db.models import Candle, PerpMetric
from app.db.session import new_session


def load_candles(
    session: Session, symbol: str, interval: str, limit: int = 400
) -> list[tuple[object, ...]]:
    stmt = (
        select(Candle.open_time, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume)
        .where(Candle.symbol == symbol, Candle.interval == interval)
        .order_by(Candle.open_time.desc())
        .limit(limit)
    )
    return [tuple(r) for r in session.execute(stmt).all()]


def load_perp(session: Session, symbol: str) -> PerpInputs | None:
    latest = session.execute(
        select(PerpMetric)
        .where(PerpMetric.symbol == symbol)
        .order_by(PerpMetric.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return None
    day_ago = latest.ts - timedelta(hours=24)
    earlier = session.execute(
        select(PerpMetric)
        .where(PerpMetric.symbol == symbol, PerpMetric.ts <= day_ago)
        .order_by(PerpMetric.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    oi_change = (
        float(latest.open_interest_value / earlier.open_interest_value - 1)
        if earlier is not None and earlier.open_interest_value
        else 0.0
    )
    return PerpInputs(funding_rate=float(latest.funding_rate), oi_change_24h=oi_change)


def run(cfg: Config, interval: str = "240") -> list[AgentOutput]:
    outputs: list[AgentOutput] = []
    with new_session() as session:
        age = data_age(session, "bybit.candles")
        age_min = int(age.total_seconds() // 60) if age is not None else 10_000
        for asset in cfg.trading.assets:
            symbol = cfg.symbol(asset)
            rows = load_candles(session, symbol, interval)
            perp = load_perp(session, cfg.symbol(asset, "USDT"))
            outputs.append(evaluate(asset, candles_frame(rows), interval, perp, age_min))  # type: ignore[arg-type]
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", default="240", choices=["15", "60", "240", "D"])
    args = parser.parse_args(argv)
    print(f"indicators {datetime.now(UTC):%Y-%m-%d %H:%M} UTC, interval {args.interval}")
    for out in run(get_config(), args.interval):
        flag = "" if out.valid else "  (STALE)"
        print(
            f"{out.asset:8s} score {out.score:+.2f}  conf {out.confidence:.2f}  "
            f"age {out.data_age_min} min{flag}"
        )
        for line in out.evidence:
            print(f"    {line}")
        for line in out.risk_flags:
            print(f"    ! {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
