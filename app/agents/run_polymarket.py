"""Run the Polymarket agent for each asset from stored markets and prices.

python -m app.agents.run_polymarket
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from sqlalchemy import select

from app.agents.polymarket import evaluate
from app.agents.schema import AgentOutput
from app.config import Config, get_config
from app.data.fetch import data_age
from app.db.models import Candle
from app.db.session import new_session


def spot_price(cfg: Config, asset: str) -> float:
    with new_session() as session:
        close = session.execute(
            select(Candle.close)
            .where(Candle.symbol == cfg.symbol(asset), Candle.interval == "15")
            .order_by(Candle.open_time.desc())
            .limit(1)
        ).scalar_one_or_none()
    return float(close) if close is not None else 0.0


def run(cfg: Config) -> list[AgentOutput]:
    outputs = []
    with new_session() as session:
        age = data_age(session, "polymarket")
        age_min = int(age.total_seconds() // 60) if age is not None else 10_000
        for asset in cfg.trading.assets:
            outputs.append(evaluate(session, asset, spot_price(cfg, asset), age_min))
    return outputs


def main() -> int:
    print(f"polymarket {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
    for out in run(get_config()):
        print(
            f"{out.asset:8s} score {out.score:+.2f}  conf {out.confidence:.2f}  "
            f"age {out.data_age_min} min"
        )
        for line in out.evidence:
            print(f"    {line}")
        for line in out.risk_flags:
            print(f"    ! {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
