"""X sentiment agent (SPEC §6).

Reads (billed, capped per cycle): a recent search per asset plus the curated accounts.
Labels: ``claude-sonnet-5-5`` returns asset, stance, kind, credibility and a shock flag per
post; the output is a schema, stored in ``x_posts``. Raw text goes to the model and the
table only (non-negotiable 5).

Aggregation in code, per asset over the labelled posts of the last 24 hours:
- weight = credibility × kind weight (news 1.0, analysis 0.8, other 0.4, shill 0.2)
- level = weighted mean of stance (+1 / 0 / −1) in [-1, 1]
- one-sidedness: when more than 85% of the weight is on one side with at least 8 posts,
  the level is pulled back by half (crowded trades reverse: contrarian caution)
- news shock: any post flagged shock with credibility ≥ 0.6 in the last 6 hours sets a
  risk flag and lifts confidence; the triggered cycle in SPEC §7 keys off this flag
- confidence from post count and mean credibility; SPX6900 gets a 1.5× count weight
  because sentiment is its main driver
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.schema import AgentOutput, Horizon
from app.config import Config
from app.data.x import XClient, XPost
from app.db.models import XPostRecord
from app.llm import LLMError, Prompt, complete

AGENT = "x_sentiment"
TASK = "x_sentiment"
LABEL_BATCH = 30
WINDOW_H = 24
SHOCK_WINDOW_H = 6
KIND_WEIGHT = {"news": 1.0, "analysis": 0.8, "other": 0.4, "shill": 0.2}
STANCE_VALUE = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
ONE_SIDED_SHARE = 0.85
ONE_SIDED_MIN_POSTS = 8
PRIOR_WEIGHT = 1.0  # credibility-weight of the neutral prior
SEARCH_QUERY = {
    "BTC": "(bitcoin OR $BTC) lang:en -is:retweet -is:reply",
    "ETH": "(ethereum OR $ETH) lang:en -is:retweet -is:reply",
    "SOL": "(solana OR $SOL) lang:en -is:retweet -is:reply",
    "BNB": '($BNB OR "BNB chain") lang:en -is:retweet -is:reply',
    "SPX6900": "(SPX6900 OR $SPX) lang:en -is:retweet -is:reply",
}


class Stance(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class Kind(StrEnum):
    NEWS = "news"
    ANALYSIS = "analysis"
    SHILL = "shill"
    OTHER = "other"


class PostLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    asset: str  # BTC/ETH/SOL/BNB/SPX6900/none; validated against config in code
    stance: Stance
    kind: Kind
    credibility: float = Field(ge=0, le=1)
    shock: bool


class LabelBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    posts: list[PostLabel]


# --- reads ---------------------------------------------------------------------


def reads_today(session: Session, now: datetime) -> int:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        session.execute(
            select(func.count()).select_from(XPostRecord).where(XPostRecord.fetched_at >= day_start)
        ).scalar_one()
    )


async def fetch_posts(session: Session, cfg: Config, client: XClient, now: datetime) -> int:
    """Search per asset within the cycle cap and the daily budget. Returns new posts stored."""
    budget_left = cfg.budget.x_reads_per_day - reads_today(session, now)
    allowance = min(cfg.x.reads_per_cycle_max, budget_left)
    if allowance < 10:
        return 0
    stored = 0

    def store(posts: list[XPost], query_asset: str | None, author: str | None) -> int:
        new = 0
        for p in posts:
            if session.get(XPostRecord, p.id) is not None:
                continue
            session.add(
                XPostRecord(
                    id=p.id,
                    author_id=p.author_id,
                    author=author,
                    created_at=p.created_at,
                    fetched_at=now,
                    query_asset=query_asset,
                    text=p.text,
                )
            )
            new += 1
        return new

    # Half of the allowance to the curated accounts (quality), half to search (breadth).
    accounts = [a for a in cfg.x.accounts if a]
    account_share = allowance // 2 if accounts else 0
    per_account = max(5, account_share // max(len(accounts), 1)) if accounts else 0
    for handle in accounts:
        if account_share < 5:
            break
        try:
            user = await client.user_by_username(handle)
            posts = await client.user_timeline(user.id, per_account)
        except Exception:  # one bad handle must not stop the cycle
            continue
        account_share -= max(len(posts), 5)
        allowance -= max(len(posts), 5)
        stored += store(posts, None, handle)

    per_asset = max(10, min(cfg.x.search_per_asset, allowance // len(cfg.trading.assets)))
    for asset in cfg.trading.assets:
        if allowance < 10:
            break
        posts = await client.search_recent(SEARCH_QUERY[asset], max_results=per_asset)
        allowance -= max(len(posts), 10)  # the API bills the minimum page even when fewer return
        stored += store(posts, asset, None)
    session.commit()
    return stored


# --- labels --------------------------------------------------------------------


async def label_unlabeled(
    session: Session, cfg: Config, prompt: Prompt, cycle_id: int | None = None
) -> int:
    rows = session.scalars(
        select(XPostRecord)
        .where(XPostRecord.labeled_at.is_(None))
        .order_by(XPostRecord.fetched_at)
        .limit(300)
    ).all()
    assets = set(cfg.trading.assets)
    done = 0
    for start in range(0, len(rows), LABEL_BATCH):
        batch = rows[start : start + LABEL_BATCH]
        by_id = {r.id: r for r in batch}
        payload = json.dumps([{"id": r.id, "text": r.text[:600]} for r in batch])
        try:
            result = await complete(
                TASK, LabelBatch, prompt=prompt, user_text=payload, cycle_id=cycle_id
            )
        except LLMError:
            continue  # stays unlabeled; logged by the LLM layer
        now = datetime.now(UTC)
        for label in result.parsed.posts:
            rec = by_id.get(label.id)
            if rec is None:
                continue
            rec.asset = label.asset if label.asset in assets else None
            rec.stance = label.stance.value
            rec.kind = label.kind.value
            rec.credibility = Decimal(str(round(label.credibility, 3)))
            rec.shock = label.shock
            rec.labeled_at = now
            done += 1
        session.commit()
    return done


# --- aggregation ------------------------------------------------------------------


def evaluate(
    session: Session, asset: str, data_age_min: int, now: datetime | None = None
) -> AgentOutput:
    now = now or datetime.now(UTC)
    rows = session.scalars(
        select(XPostRecord).where(
            XPostRecord.asset == asset,
            XPostRecord.labeled_at.is_not(None),
            XPostRecord.created_at >= now - timedelta(hours=WINDOW_H),
        )
    ).all()
    return aggregate(list(rows), asset, data_age_min, now)


def aggregate(rows: list[XPostRecord], asset: str, data_age_min: int, now: datetime) -> AgentOutput:
    """Pure aggregation of labelled posts into the agent output (testable without a DB)."""
    weights = [float(r.credibility or 0) * KIND_WEIGHT.get(r.kind or "other", 0.4) for r in rows]
    stances = [STANCE_VALUE.get(r.stance or "neutral", 0.0) for r in rows]
    total = sum(weights)
    # Shrink toward zero when little credible weight exists (two shill posts are not a view).
    level = sum(w * s for w, s in zip(weights, stances, strict=True)) / (total + PRIOR_WEIGHT)
    risk_flags: list[str] = []
    n = len(rows)

    bull = sum(w for w, s in zip(weights, stances, strict=True) if s > 0)
    bear = sum(w for w, s in zip(weights, stances, strict=True) if s < 0)
    sided = max(bull, bear) / (bull + bear) if bull + bear else 0.0
    if n >= ONE_SIDED_MIN_POSTS and sided >= ONE_SIDED_SHARE:
        level *= 0.5
        risk_flags.append(f"one-sided sentiment ({sided:.0%} one way)")

    shocks = [
        r
        for r in rows
        if r.shock
        and float(r.credibility or 0) >= 0.6
        and r.created_at
        and r.created_at >= now - timedelta(hours=SHOCK_WINDOW_H)
    ]
    if shocks:
        risk_flags.append(f"news shock: {len(shocks)} credible post(s) in {SHOCK_WINDOW_H}h")

    count_weight = 1.5 if asset == "SPX6900" else 1.0
    mean_cred = (sum(float(r.credibility or 0) for r in rows) / n) if n else 0.0
    confidence = min(1.0, (1 - math.exp(-count_weight * n / 15)) * (0.4 + 0.6 * mean_cred))
    if shocks:
        confidence = min(1.0, confidence + 0.2)

    kinds = {k: sum(1 for r in rows if r.kind == k) for k in KIND_WEIGHT}
    evidence = [
        f"{n} posts in {WINDOW_H}h: {kinds['news']} news, {kinds['analysis']} analysis, "
        f"{kinds['shill']} shill; mean credibility {mean_cred:.2f}",
        f"bullish weight {bull:.1f} vs bearish {bear:.1f} → level {level:+.2f}",
    ]
    if n == 0:
        evidence = ["no labelled posts for this asset in the window"]
    return AgentOutput(
        agent=AGENT,
        asset=asset,
        score=max(-1.0, min(1.0, level)),
        confidence=confidence,
        horizon=Horizon.D1,
        evidence=evidence,
        risk_flags=risk_flags,
        data_age_min=data_age_min,
    )
