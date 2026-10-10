"""X sentiment: fetch (billed), label, aggregate per asset.

python -m app.agents.run_x            # fetch + label + score
python -m app.agents.run_x --no-fetch # label what is stored, then score (no X reads)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.agents import x_sentiment as xs
from app.agents.schema import AgentOutput
from app.config import Config, get_config, get_secrets
from app.data.x import XClient
from app.db.models import XPostRecord
from app.db.session import new_session
from app.llm import load_prompt


async def run(cfg: Config, *, fetch: bool = True, cycle_id: int | None = None) -> list[AgentOutput]:
    now = datetime.now(UTC)
    with new_session() as session:
        if fetch:
            async with XClient(get_secrets().x_bearer_token) as client:
                await xs.fetch_posts(session, cfg, client, now)
        await xs.label_unlabeled(session, cfg, load_prompt("x_sentiment"), cycle_id)
        newest = session.execute(select(func.max(XPostRecord.fetched_at))).scalar_one_or_none()
        age_min = int((now - newest).total_seconds() // 60) if newest else 10_000
        return [xs.evaluate(session, asset, age_min, now) for asset in cfg.trading.assets]


def run_shadow(cfg: Config) -> list[AgentOutput]:
    """X sentiment for the Jev-labelled assets, aggregated from the shadow labeller's
    labels (``x_post_labels``) instead of the posts' own; variant ``alt`` for the IC."""
    now = datetime.now(UTC)
    with new_session() as session:
        assets = [
            a for a in cfg.trading.assets if xs.labeler_for_asset(cfg, a) != cfg.models.x_sentiment
        ]
        if not assets or not xs.shadow_on(session, cfg):
            return []
        newest = session.execute(select(func.max(XPostRecord.fetched_at))).scalar_one_or_none()
        age_min = int((now - newest).total_seconds() // 60) if newest else 10_000
        return [xs.evaluate_shadow(session, asset, age_min, now) for asset in assets]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-fetch", action="store_true", help="skip the billed X reads")
    args = parser.parse_args(argv)
    print(f"x_sentiment {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
    for out in asyncio.run(run(get_config(), fetch=not args.no_fetch)):
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
