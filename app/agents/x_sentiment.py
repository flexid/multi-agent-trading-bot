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
- confidence from post count and mean credibility; memecoins (DOGE) get a 1.5× count weight
  because sentiment is its main driver
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.jev_questions import CREDIBILITY_LEVELS, post_questions
from app.agents.jev_questions import VERSION as QUESTIONS_VERSION
from app.agents.schema import AgentOutput, Horizon
from app.config import Config
from app.data.x import XClient, XPost
from app.db.models import RiskState, XPostLabel, XPostRecord
from app.llm import LLMError, Prompt, complete, is_jev, judge

log = logging.getLogger(__name__)

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
    "XRP": "(xrp OR $XRP OR ripple) lang:en -is:retweet -is:reply",
    "DOGE": "(dogecoin OR $DOGE) lang:en -is:retweet -is:reply",
    "PEPE": "($PEPE OR pepecoin) lang:en -is:retweet -is:reply",
    "HBAR": "($HBAR OR hedera) lang:en -is:retweet -is:reply",
    "PUMP": '($PUMP OR "pump fun" OR pumpfun) lang:en -is:retweet -is:reply',
    "ENA": "($ENA OR ethena) lang:en -is:retweet -is:reply",
    "BNB": '($BNB OR "BNB chain") lang:en -is:retweet -is:reply',
    "SPX6900": "(SPX6900 OR $SPX) lang:en -is:retweet -is:reply",
}
# Mention volume ("buzz") is counted with the counts endpoint: one request per asset per
# cycle, no post text. $SPX is SPX6900's ticker (owner, 2026-10-09); this is a crypto bot.
MENTION_QUERY = {
    "BTC": "(bitcoin OR $BTC) -is:retweet",
    "ETH": "(ethereum OR $ETH) -is:retweet",
    "SOL": "(solana OR $SOL) -is:retweet",
    "XRP": "($XRP OR #XRP OR xrp) -is:retweet",
    "DOGE": "($DOGE OR #DOGE OR dogecoin) -is:retweet",
    "PEPE": "($PEPE OR #PEPE OR pepecoin) -is:retweet",
    "HBAR": "($HBAR OR #HBAR OR hedera) -is:retweet",
    "PUMP": '($PUMP OR #PUMP OR "pump fun" OR pumpfun) -is:retweet',
    "ENA": "($ENA OR #ENA OR ethena) -is:retweet",
    "BNB": "($BNB OR #BNB) -is:retweet",
    "SPX6900": "($SPX OR SPX6900 OR #SPX6900) -is:retweet",
}
# Plain-language names for the Jev asset choice (a literal reader: name the confusions).
ASSET_TEXT = {
    "BTC": "Bitcoin (ticker BTC)",
    "ETH": "Ethereum (ticker ETH)",
    "SOL": "Solana (ticker SOL)",
    "XRP": "XRP, Ripple's token (ticker XRP)",
    "DOGE": "Dogecoin (ticker DOGE)",
    "PEPE": "Pepe, the frog memecoin (ticker PEPE)",
    "HBAR": "Hedera, the Hedera Hashgraph token (ticker HBAR)",
    "PUMP": "the pump.fun launchpad token (ticker PUMP)",
    "SPX6900": "the SPX6900 memecoin (ticker SPX; not the S&P 500 index)",
    "ENA": "Ethena, the synthetic dollar protocol token (ticker ENA)",
}
MEME_ASSETS = {
    "DOGE",
    "SPX6900",
    "PEPE",
    "PUMP",
}  # attention is the main driver: count and buzz weigh more
MENTION_SERIES = "XMENTIONS_{asset}"  # hourly counts in macro_observations
BUZZ_BASELINE_DAYS = 6
BUZZ_SPIKE, BUZZ_FADE = 2.0, 0.5


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
    asset: str  # one of the configured assets or none; validated against config in code
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

    # A third of the allowance to the curated accounts (quality), the rest to search
    # (breadth); the asset order rotates per cycle so every asset gets its turn when the
    # allowance does not cover all ten (2026-10-10: the alts never got searched).
    accounts = [a for a in cfg.x.accounts if a]
    account_share = allowance // 3 if accounts else 0
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
    for asset in search_order(cfg.trading.assets, now, cfg.trading.cycle_hours):
        if allowance < 10:
            break
        posts = await client.search_recent(SEARCH_QUERY[asset], max_results=per_asset)
        allowance -= max(len(posts), 10)  # the API bills the minimum page even when fewer return
        stored += store(posts, asset, None)
    await count_mentions(session, cfg, client, now)
    session.commit()
    return stored


def search_order(assets: list[str], now: datetime, cycle_hours: int) -> list[str]:
    """The assets rotated by the cycle slot, so a short allowance reaches a different
    first asset each cycle."""
    if not assets:
        return []
    slot = int(now.timestamp() // (max(cycle_hours, 1) * 3600))
    k = slot % len(assets)
    return assets[k:] + assets[:k]


async def count_mentions(session: Session, cfg: Config, client: XClient, now: datetime) -> int:
    """Hourly mention counts per asset for the last 7 days, upserted as
    ``XMENTIONS_<asset>`` in macro_observations. One request per asset; a failure (tier
    without the counts endpoint, rate limit) leaves the series as it was."""
    from sqlalchemy.dialects.postgresql import insert

    from app.db.models import MacroObservation

    done = 0
    for asset in cfg.trading.assets:
        try:
            buckets = await client.counts_recent(MENTION_QUERY[asset], granularity="hour")
        except Exception as exc:  # the agent still works on sentiment alone
            log.warning("mention counts %s failed: %s", asset, exc)
            continue
        rows = [
            {
                "series": MENTION_SERIES.format(asset=asset),
                "date": start,
                "value": Decimal(count),
                "fetched_at": now,
            }
            for start, count in buckets
            if start < now.replace(minute=0, second=0, microsecond=0)  # full hours only
        ]
        if rows:
            stmt = insert(MacroObservation).values(rows)
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["series", "date"],
                    set_={"value": stmt.excluded.value, "fetched_at": stmt.excluded.fetched_at},
                )
            )
            done += 1
    return done


def mention_volume(session: Session, asset: str, now: datetime) -> tuple[int, float] | None:
    """(mentions in the last 24 h, mentions per 24 h over the 6 days before) or None
    when the series is too short to compare."""
    from app.db.models import MacroObservation

    since = now - timedelta(days=BUZZ_BASELINE_DAYS + 1)
    rows = session.execute(
        select(MacroObservation.date, MacroObservation.value).where(
            MacroObservation.series == MENTION_SERIES.format(asset=asset),
            MacroObservation.date >= since,
        )
    ).all()
    cut = now - timedelta(hours=WINDOW_H)
    last = sum(int(v) for d, v in rows if d >= cut)
    base_rows = [(d, v) for d, v in rows if d < cut]
    if len(base_rows) < 48:  # at least two full days of history to compare against
        return None
    baseline = sum(int(v) for _, v in base_rows) / len(base_rows) * WINDOW_H
    return last, baseline


# --- labels --------------------------------------------------------------------


def labeler_for(cfg: Config, rec: XPostRecord) -> str:
    """The model that labels a post: the sleeve's ``x_sentiment`` override when the post
    was searched for one of its assets or comes from a catalyst account (owner 2026-10-10:
    Jev for the alt sleeve), else the task's model."""
    sleeve = cfg.sleeve_cfg(rec.query_asset) if rec.query_asset else None
    if sleeve is None and rec.author and rec.author in cfg.catalysts.accounts:
        for s in cfg.sleeves.values():
            if s.models.get(TASK):
                sleeve = s
                break
    return (sleeve.models.get(TASK) if sleeve else None) or cfg.models.x_sentiment


def labeler_for_asset(cfg: Config, asset: str) -> str:
    sleeve = cfg.sleeve_cfg(asset)
    return (sleeve.models.get(TASK) if sleeve else None) or cfg.models.x_sentiment


def shadow_on(session: Session, cfg: Config) -> bool:
    return _shadow_on(session, cfg)


def _shadow_on(session: Session, cfg: Config) -> bool:
    state = session.get(RiskState, 1)
    return bool(cfg.catalysts.shadow_model) and (state is None or state.mode != "live")


async def label_unlabeled(
    session: Session, cfg: Config, prompt: Prompt, cycle_id: int | None = None
) -> int:
    rows = session.scalars(
        select(XPostRecord)
        .where(XPostRecord.labeled_at.is_(None))
        .order_by(XPostRecord.fetched_at)
        .limit(300)
    ).all()
    by_model: dict[str, list[XPostRecord]] = {}
    for rec in rows:
        by_model.setdefault(labeler_for(cfg, rec), []).append(rec)
    done = 0
    for model, group in by_model.items():
        if is_jev(model):
            done += await _label_with_jev(session, cfg, group, model, prompt, cycle_id)
        else:
            done += await _label_with_model(session, cfg, group, model, prompt, cycle_id)
    return done


def _apply(rec: XPostRecord, label: PostLabel, assets: set[str], now: datetime) -> None:
    rec.asset = label.asset if label.asset in assets else None
    rec.stance = label.stance.value
    rec.kind = label.kind.value
    rec.credibility = Decimal(str(round(label.credibility, 3)))
    rec.shock = label.shock
    rec.labeled_at = now


def _shadow_row(
    rec: XPostRecord, label: PostLabel, labeler: str, model: str, now: datetime
) -> XPostLabel:
    return XPostLabel(
        post_id=rec.id,
        labeler=labeler,
        model=model,
        asset=label.asset if label.asset != "none" else None,
        stance=label.stance.value,
        kind=label.kind.value,
        credibility=Decimal(str(round(label.credibility, 3))),
        shock=label.shock,
        labeled_at=now,
    )


async def _label_with_model(
    session: Session,
    cfg: Config,
    rows: list[XPostRecord],
    model: str,
    prompt: Prompt,
    cycle_id: int | None,
    *,
    apply: bool = True,
    labeler: str | None = None,
) -> int:
    """Label ``rows`` with a text model. ``apply`` writes the labels on the posts; with
    ``labeler`` the labels are also kept in ``x_post_labels`` for the comparison."""
    assets = set(cfg.trading.assets)
    done = 0
    for start in range(0, len(rows), LABEL_BATCH):
        batch = rows[start : start + LABEL_BATCH]
        by_id = {r.id: r for r in batch}
        payload = json.dumps([{"id": r.id, "text": r.text[:600]} for r in batch])
        try:
            result = await complete(
                TASK, LabelBatch, prompt=prompt, user_text=payload, cycle_id=cycle_id, model=model
            )
        except LLMError:
            continue  # stays unlabeled; logged by the LLM layer
        now = datetime.now(UTC)
        for label in result.parsed.posts:
            rec = by_id.get(label.id)
            if rec is None:
                continue
            if apply:
                _apply(rec, label, assets, now)
            if labeler:
                session.merge(_shadow_row(rec, label, labeler, result.model, now))
            done += 1
        session.commit()
    return done


JEV_BATCH = 10  # posts per Jev request (five questions each; a small state reads better)


def _post_label_from_answers(post_id: str, answers: dict[str, Any], i: int) -> PostLabel:
    a, s, k, c, sh = (answers[f"p{i}_{q}"] for q in ("asset", "stance", "kind", "cred", "shock"))
    # credibility: the probability-weighted level over four levels → 0..1
    cred = max(0.0, min(1.0, float(c["score"]) / (len(CREDIBILITY_LEVELS) - 1)))
    return PostLabel(
        id=post_id,
        asset=str(a["choice"]),
        stance=Stance(str(s["choice"])),
        kind=Kind(str(k["choice"])),
        credibility=cred,
        shock=float(sh["noul"]) >= 0.5,
    )


async def _label_with_jev(
    session: Session,
    cfg: Config,
    rows: list[XPostRecord],
    model: str,
    prompt: Prompt,
    cycle_id: int | None,
) -> int:
    """Jev labels the posts (one request per ``JEV_BATCH`` posts, five questions each);
    while not live, the shadow text model labels the same posts into ``x_post_labels``."""
    assets = set(cfg.trading.assets)
    asset_text = {a: ASSET_TEXT.get(a, a) for a in cfg.trading.assets}
    done = 0
    labelled: list[XPostRecord] = []
    for start in range(0, len(rows), JEV_BATCH):
        batch = rows[start : start + JEV_BATCH]
        state = {"posts": [{"text": r.text[:600]} for r in batch]}
        try:
            result = await judge(
                TASK,
                version=QUESTIONS_VERSION,
                state=state,
                questions=post_questions(len(batch), asset_text),
                model=model,
                cycle_id=cycle_id,
                asset=batch[0].query_asset if len({r.query_asset for r in batch}) == 1 else None,
            )
        except LLMError:
            continue  # fail closed: the posts stay unlabeled
        now = datetime.now(UTC)
        for i, rec in enumerate(batch):
            try:
                label = _post_label_from_answers(rec.id, result.answers, i)
            except (KeyError, ValueError):
                continue
            _apply(rec, label, assets, now)
            session.merge(_shadow_row(rec, label, "jev", result.model, now))
            labelled.append(rec)
            done += 1
        session.commit()
    if labelled and _shadow_on(session, cfg):
        await _label_with_model(
            session,
            cfg,
            labelled,
            cfg.catalysts.shadow_model,
            prompt,
            cycle_id,
            apply=False,
            labeler="sonnet",
        )
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
    return aggregate(list(rows), asset, data_age_min, now, mention_volume(session, asset, now))


def evaluate_shadow(
    session: Session, asset: str, data_age_min: int, now: datetime | None = None
) -> AgentOutput:
    """The same aggregation over the shadow labeller's labels of the same posts."""
    now = now or datetime.now(UTC)
    pairs = session.execute(
        select(XPostRecord, XPostLabel)
        .join(XPostLabel, XPostLabel.post_id == XPostRecord.id)
        .where(
            XPostLabel.labeler == "sonnet",
            XPostLabel.asset == asset,
            XPostRecord.created_at >= now - timedelta(hours=WINDOW_H),
        )
    ).all()
    rows = [
        XPostRecord(
            id=rec.id,
            text="",
            fetched_at=rec.fetched_at,
            created_at=rec.created_at,
            asset=lab.asset,
            stance=lab.stance,
            kind=lab.kind,
            credibility=lab.credibility,
            shock=lab.shock,
            labeled_at=lab.labeled_at,
        )
        for rec, lab in pairs
    ]
    return aggregate(rows, asset, data_age_min, now, mention_volume(session, asset, now))


def aggregate(
    rows: list[XPostRecord],
    asset: str,
    data_age_min: int,
    now: datetime,
    mentions: tuple[int, float] | None = None,
) -> AgentOutput:
    """Pure aggregation of labelled posts into the agent output (testable without a DB).

    ``mentions``: (last 24 h, baseline per 24 h). Attention scales confidence, not the
    direction: twice the usual volume lifts it, half the usual volume lowers it, and
    memecoins react twice as much because attention is their main driver (owner 2026-10-09).
    A spike or a fade is also a risk flag the PMs see."""
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

    count_weight = 1.5 if asset in MEME_ASSETS else 1.0
    mean_cred = (sum(float(r.credibility or 0) for r in rows) / n) if n else 0.0
    confidence = min(1.0, (1 - math.exp(-count_weight * n / 15)) * (0.4 + 0.6 * mean_cred))
    if shocks:
        confidence = min(1.0, confidence + 0.2)
    buzz_line: str | None = None
    if mentions is not None and mentions[1] > 0:
        last, baseline = mentions
        buzz = min(3.0, last / baseline)
        swing = 0.5 if asset in MEME_ASSETS else 0.25
        confidence = min(1.0, confidence * (1 + swing * (min(buzz, 2.0) - 1.0)))
        buzz_line = (
            f"mentions: {last} in {WINDOW_H}h vs {baseline:.0f} per day lately ({buzz:.1f}×)"
        )
        if buzz >= BUZZ_SPIKE:
            risk_flags.append(f"attention spike ({buzz:.1f}× the usual mention volume)")
        elif buzz <= BUZZ_FADE:
            risk_flags.append(f"attention fading ({buzz:.1f}× the usual mention volume)")

    kinds = {k: sum(1 for r in rows if r.kind == k) for k in KIND_WEIGHT}
    evidence = [
        f"{n} posts in {WINDOW_H}h: {kinds['news']} news, {kinds['analysis']} analysis, "
        f"{kinds['shill']} shill; mean credibility {mean_cred:.2f}",
        f"bullish weight {bull:.1f} vs bearish {bear:.1f} → level {level:+.2f}",
    ]
    if n == 0:
        evidence = ["no labelled posts for this asset in the window"]
    if buzz_line:
        evidence.append(buzz_line)
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
