"""Catalyst agent (Djaf) for the alt sleeve: read the project and alt-news accounts on X
(billed), classify the pending events with Jev (Sonnet beside it while not live), then
score every sleeve asset from the labels.

python -m app.agents.run_catalysts            # fetch + classify + score
python -m app.agents.run_catalysts --no-fetch # classify what is stored, then score
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents import catalysts as cat
from app.agents.schema import AgentOutput
from app.agents.x_sentiment import reads_today
from app.config import Config, get_config, get_secrets
from app.data.fetch import data_age
from app.data.x import XClient, XPost
from app.db.models import CatalystEvent, RiskState, XPostRecord
from app.db.session import new_session

log = logging.getLogger(__name__)
POSTS_PER_ACCOUNT = 5  # the API's minimum page; project accounts post rarely


def in_shadow(session: Session) -> bool:
    state = session.get(RiskState, 1)
    return state is None or state.mode != "live"


async def fetch_accounts(session: Session, cfg: Config, client: XClient, now: datetime) -> int:
    """Latest posts of the catalyst accounts, within this agent's share of the daily X
    budget. Each post is stored once in ``x_posts`` (it also feeds the sentiment labels)
    and, when it names a sleeve asset, becomes a pending catalyst event."""
    assets = cat.sleeve_assets(cfg)
    budget_left = cfg.budget.x_reads_per_day - reads_today(session, now)
    allowance = min(cfg.catalysts.reads_per_cycle_max, budget_left)
    stored = 0
    for handle in cfg.catalysts.accounts:
        if allowance < POSTS_PER_ACCOUNT:
            break
        try:
            user = await client.user_by_username(handle)
            posts: list[XPost] = await client.user_timeline(user.id, POSTS_PER_ACCOUNT)
        except Exception as exc:  # one bad handle must not stop the cycle
            log.warning("catalyst account %s failed: %s", handle, exc)
            continue
        allowance -= max(len(posts), POSTS_PER_ACCOUNT)
        for p in posts:
            if session.get(XPostRecord, p.id) is None:
                session.add(
                    XPostRecord(
                        id=p.id,
                        author_id=p.author_id,
                        author=handle,
                        created_at=p.created_at,
                        fetched_at=now,
                        query_asset=None,
                        text=p.text,
                    )
                )
            hint = cat.mentioned_asset(p.text, assets)
            if hint is None:
                continue
            exists = session.execute(
                select(CatalystEvent.id).where(
                    CatalystEvent.source == "x", CatalystEvent.source_id == p.id
                )
            ).first()
            if exists:
                continue
            session.add(
                CatalystEvent(
                    source="x",
                    source_id=p.id,
                    author=handle,
                    url=f"https://x.com/{handle}/status/{p.id}",
                    text=p.text[:2000],
                    event_at=p.created_at or now,
                    fetched_at=now,
                    hint_asset=hint,
                )
            )
            stored += 1
    session.commit()
    return stored


async def run(cfg: Config, *, fetch: bool = True, cycle_id: int | None = None) -> list[AgentOutput]:
    """Main outputs for the sleeve's assets. The shadow outputs (scored from Sonnet's
    labels) are returned by ``run_shadow`` and stored as variant ``alt``."""
    assets = cat.sleeve_assets(cfg)
    if not assets:
        return []
    now = datetime.now(UTC)
    with new_session() as session:
        if fetch:
            async with XClient(get_secrets().x_bearer_token) as client:
                await fetch_accounts(session, cfg, client, now)
        await cat.classify_pending(session, cfg, shadow=in_shadow(session), cycle_id=cycle_id)
        return [cat.evaluate(session, a, _age_min(session, now), now) for a in assets]


def run_shadow(cfg: Config) -> list[AgentOutput]:
    assets = cat.sleeve_assets(cfg)
    now = datetime.now(UTC)
    with new_session() as session:
        if not assets or not in_shadow(session):
            return []
        age = _age_min(session, now)
        return [cat.evaluate(session, a, age, now, shadow=True) for a in assets]


def _age_min(session: Session, now: datetime) -> int:
    """Freshness: the newest event fetched or the newest announcement/unlock fetch,
    whichever is more recent. No sources at all → stale."""
    newest = session.execute(select(func.max(CatalystEvent.fetched_at))).scalar_one_or_none()
    ages = [int((now - newest).total_seconds() // 60)] if newest else []
    for source in ("catalysts.bybit", "catalysts.unlocks"):
        age = data_age(session, source, now)
        if age is not None:
            ages.append(int(age.total_seconds() // 60))
    return min(ages) if ages else 10_000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-fetch", action="store_true", help="skip the billed X reads")
    args = parser.parse_args(argv)
    print(f"catalysts {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
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
