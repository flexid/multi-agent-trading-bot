"""Run the macro agent: one regime call, then an output per asset scaled by coupling.

python -m app.agents.run_macro
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from app.agents import macro
from app.agents.schema import AgentOutput
from app.config import Config, get_config
from app.data.fetch import data_age
from app.db.session import new_session
from app.llm import load_prompt


async def run(cfg: Config, cycle_id: int | None = None) -> list[AgentOutput]:
    now = datetime.now(UTC)
    with new_session() as session:
        inputs, wide = macro.build_inputs(session, now)
        age = data_age(session, "macro.fred")
        age_min = int(age.total_seconds() // 60) if age is not None else 10_000
        couplings = {
            asset: macro.coupling(
                macro.asset_daily_closes(session, cfg.symbol(asset), macro.HISTORY_DAYS), wide
            )
            for asset in cfg.trading.assets
        }
    view = await macro.read_regime(inputs, load_prompt("macro"), cycle_id)
    return [
        macro.to_output(asset, view, couplings[asset], inputs, age_min)
        for asset in cfg.trading.assets
    ]


def main() -> int:
    print(f"macro {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
    outputs = asyncio.run(run(get_config()))
    for i, out in enumerate(outputs):
        print(
            f"{out.asset:8s} score {out.score:+.2f}  conf {out.confidence:.2f}  {out.evidence[0]}"
        )
        if i == 0:
            for line in out.evidence[1:]:
                print(f"    {line}")
            for line in out.risk_flags:
                print(f"    ! {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
