"""Catalyst agent for the alt sleeve (owner 2026-10-10), crew name Djaf.

Sources: posts from the official project accounts and curated alt-news accounts on X,
Bybit announcements (listings, delistings, margin and borrow changes) and the token-unlock
calendar (DefiLlama's public emissions datasets, see DECISIONS).

Jev (TypeSafe) classifies every text event: asset (choice), type (listing, delisting,
unlock, hack, partnership, other), direction (bullish, bearish, neutral) and materiality
(the probability it moves price > 3% in 48 h). Unlocks are structured data and are
labelled by code (Jev is not for arithmetic). Jev only classifies; the thresholds and the
score are code:

    score(asset) = clamp(Σ sign(direction) × materiality × type weight × decay(age), -1, 1)

Text events decay exponentially from the moment they were published, with a half-life per
type (a hack stays relevant longer than a partnership post). An unlock is a scheduled
event: its weight ramps up over the week before the date and decays over the three days
after; its materiality is the share of supply released (2% in one go = 1.0).

While the process is not live, claude-sonnet-5-5 labels the same events in parallel
(``shadow``); the admin shows agreement and the IC of both.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.jev_questions import VERSION as QUESTIONS_VERSION
from app.agents.jev_questions import event_questions
from app.agents.schema import AgentOutput, Horizon
from app.config import Config
from app.db.models import CatalystEvent
from app.llm import LLMError, Prompt, complete, judge

log = logging.getLogger(__name__)

AGENT = "catalysts"
TASK = "catalysts"
WINDOW_DAYS = 7  # text events older than this are out of the score
MIN_ASSET_P = 0.6  # Jev's probability for the asset; below it the event is "none"
MIN_MATERIALITY = 0.2  # below it the event is logged but does not score
MAX_PER_CYCLE = 60  # events classified per cycle (cost guard; Jev is cheap, Sonnet is not)
TYPE_WEIGHT = {
    "listing": 1.0,
    "delisting": 1.0,
    "hack": 1.0,
    "unlock": 0.8,
    "partnership": 0.6,
    "other": 0.3,
}
HALF_LIFE_H = {
    "listing": 36.0,
    "delisting": 48.0,
    "hack": 48.0,
    "unlock": 24.0,
    "partnership": 24.0,
    "other": 12.0,
}
UNLOCK_RAMP_DAYS = 7  # pressure builds over the week before the unlock
UNLOCK_TAIL_DAYS = 3  # and fades in the three days after
UNLOCK_FULL_PCT = 2.0  # % of supply in one release that counts as fully material
UNLOCK_MIN_PCT = 0.1  # smaller releases (daily linear drip) are not events
DIRECTION_SIGN = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}

# Plain-language asset descriptions for the Jev asset choice (literal reader: name the
# confusions explicitly). Only the alt sleeve's assets are catalyst targets.
ASSET_TEXT: dict[str, str] = {
    "PEPE": "Pepe, the frog memecoin (ticker PEPE)",
    "HBAR": "Hedera, the Hedera Hashgraph network token (ticker HBAR)",
    "PUMP": "the pump.fun launchpad token (ticker PUMP)",
    "SPX6900": "the SPX6900 memecoin (ticker SPX; not the S&P 500 index)",
    "ENA": "Ethena, the synthetic dollar protocol token (ticker ENA)",
}
# Code-side pre-filter for announcements and posts: an event must mention an asset by
# ticker or name to reach the classifier (Jev: send only what the question needs).
ASSET_PATTERNS: dict[str, re.Pattern[str]] = {
    "PEPE": re.compile(r"\bPEPE(USD[TC])?\b|\$PEPE|pepecoin", re.I),
    "HBAR": re.compile(r"\bHBAR(USD[TC])?\b|\$HBAR|\bhedera\b", re.I),
    "PUMP": re.compile(r"\bPUMPUSD[TC]\b|\$PUMP|\bpump\.?fun\b|\bpumpfun\b|\bPUMP token\b", re.I),
    "SPX6900": re.compile(r"\bSPX6900\b|\$SPX\b|\bSPXUSD[TC]\b", re.I),
    "ENA": re.compile(r"\bENA(USD[TC])?\b|\$ENA|\bethena\b", re.I),
}
BYBIT_KINDS = ("new_crypto", "delistings", "maintenance_updates", "latest_bybit_news")


class EventType(StrEnum):
    LISTING = "listing"
    DELISTING = "delisting"
    UNLOCK = "unlock"
    HACK = "hack"
    PARTNERSHIP = "partnership"
    OTHER = "other"


class Direction(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class EventLabel(BaseModel):
    """The shadow model's answer per event (same fields as Jev's choices)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    asset: str
    type: EventType
    direction: Direction
    materiality: float = Field(ge=0, le=1)


class EventLabelBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[EventLabel]


# --- candidates -------------------------------------------------------------------


def sleeve_assets(cfg: Config) -> list[str]:
    """The assets this agent covers: those of the sleeve that lists it in its weights
    (the alts), else none."""
    for s in cfg.sleeves.values():
        if s.weights.get(AGENT, 0.0) > 0:
            return [a for a in s.assets if a in ASSET_TEXT]
    return []


def mentioned_asset(text: str, assets: list[str]) -> str | None:
    """The first configured asset the text names; None when it names none."""
    for asset in assets:
        pattern = ASSET_PATTERNS.get(asset)
        if pattern and pattern.search(text):
            return asset
    return None


# --- unlocks (structured: labelled by code) -----------------------------------------


@dataclass(frozen=True)
class Unlock:
    asset: str
    at: datetime
    tokens: float
    supply_pct: float  # of the adjusted (max minus unallocated) supply
    recipients: str


def unlocks_from_schedule(
    asset: str, data: dict[str, Any], now: datetime, horizon_days: int
) -> list[Unlock]:
    """Upcoming releases from a DefiLlama emissions dataset: cliff allocations from
    ``metadata.unlockEvents`` plus any day in ``documentedData`` whose release is at least
    ``UNLOCK_MIN_PCT`` of supply (a cliff hidden in a linear series). The daily drip of a
    linear schedule is not an event."""
    supply = float((data.get("supplyMetrics") or {}).get("adjustedSupply") or 0)
    if supply <= 0:
        supply = float((data.get("supplyMetrics") or {}).get("maxSupply") or 0)
    if supply <= 0:
        return []
    start = now - timedelta(days=UNLOCK_TAIL_DAYS)
    end = now + timedelta(days=horizon_days)
    found: dict[str, Unlock] = {}
    for event in (data.get("metadata") or {}).get("unlockEvents") or []:
        try:
            at = datetime.fromtimestamp(int(event["timestamp"]), tz=UTC)
        except (KeyError, TypeError, ValueError):
            continue
        if not start <= at <= end:
            continue
        cliffs = event.get("cliffAllocations") or []
        tokens = sum(float(c.get("amount") or 0) for c in cliffs)
        if tokens <= 0:
            continue
        pct = tokens / supply * 100
        if pct < UNLOCK_MIN_PCT:
            continue
        who = ", ".join(sorted({str(c.get("recipient") or "?") for c in cliffs}))[:120]
        found[at.date().isoformat()] = Unlock(asset, at, tokens, pct, who)
    for series in (data.get("documentedData") or {}).get("data") or []:
        points = series.get("data") or []
        for prev, cur in zip(points, points[1:], strict=False):
            try:
                at = datetime.fromtimestamp(int(cur["timestamp"]), tz=UTC)
                released = float(cur.get("unlocked") or 0) - float(prev.get("unlocked") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if not start <= at <= end or released <= 0:
                continue
            pct = released / supply * 100
            key = at.date().isoformat()
            if pct < UNLOCK_MIN_PCT or key in found:
                continue
            # the same cliff often lands a day apart in the two schedules: keep the metadata one
            if any(abs((u.at - at).total_seconds()) <= 86400 for u in found.values()):
                continue
            found[key] = Unlock(asset, at, released, pct, str(series.get("label") or "?")[:120])
    return sorted(found.values(), key=lambda u: u.at)


def unlock_materiality(supply_pct: float) -> float:
    return max(0.0, min(1.0, supply_pct / UNLOCK_FULL_PCT))


def unlock_weight(at: datetime, now: datetime) -> float:
    """Ramps linearly from 0 a week before to 1 at the unlock, then decays over 3 days."""
    days = (at - now).total_seconds() / 86400
    if days > UNLOCK_RAMP_DAYS or days < -UNLOCK_TAIL_DAYS:
        return 0.0
    if days >= 0:
        return 1 - days / UNLOCK_RAMP_DAYS
    return 1 + days / UNLOCK_TAIL_DAYS


# --- classification ----------------------------------------------------------------


def _jev_state(row: CatalystEvent) -> dict[str, Any]:
    return {
        "source": {"x": "a post on X", "bybit": "a Bybit exchange announcement"}.get(
            row.source, row.source
        ),
        "author": row.author or "",
        "published_at": row.event_at.isoformat(),
        "text": row.text[:1500],
    }


async def classify_with_jev(
    row: CatalystEvent, assets: list[str], model: str, cycle_id: int | None
) -> dict[str, Any]:
    """Jev's four answers for one event, as the stored label dict. Raises ``LLMError``."""
    result = await judge(
        TASK,
        version=QUESTIONS_VERSION,
        state=_jev_state(row),
        questions=event_questions({a: ASSET_TEXT[a] for a in assets}),
        model=model,
        cycle_id=cycle_id,
        asset=row.hint_asset,
    )
    a, t, d, m = (result.answers[k] for k in ("asset", "type", "direction", "material"))
    asset_p = float(a["probabilities"].get(a["choice"], 0.0))
    return {
        "model": result.model,
        "asset": a["choice"] if a["choice"] in assets and asset_p >= MIN_ASSET_P else None,
        "asset_p": round(asset_p, 3),
        "type": t["choice"],
        "type_confidence": round(float(t.get("confidence", 0.0)), 3),
        "direction": d["choice"],
        "materiality": round(float(m["noul"]), 3),
        "probabilities": {
            "asset": a["probabilities"],
            "type": t["probabilities"],
            "direction": d["probabilities"],
        },
    }


async def classify_with_model(
    rows: list[CatalystEvent], assets: list[str], model: str, cycle_id: int | None
) -> dict[int, dict[str, Any]]:
    """The shadow labeller (a text model, through ``complete``) over a batch of events.
    Returns labels by event id; a failed call returns nothing (logged by the LLM layer)."""
    from app.llm import load_prompt

    prompt: Prompt = load_prompt("catalysts")
    payload = json.dumps(
        [{"id": str(r.id), "source": r.source, "text": r.text[:1500]} for r in rows]
    )
    try:
        result = await complete(
            TASK, EventLabelBatch, prompt=prompt, user_text=payload, model=model, cycle_id=cycle_id
        )
    except LLMError:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for label in result.parsed.events:
        try:
            event_id = int(label.id)
        except ValueError:
            continue
        out[event_id] = {
            "model": result.model,
            "asset": label.asset if label.asset in assets else None,
            "type": label.type.value,
            "direction": label.direction.value,
            "materiality": round(label.materiality, 3),
        }
    return out


async def classify_pending(
    session: Session, cfg: Config, *, shadow: bool, cycle_id: int | None = None
) -> int:
    """Label every unclassified text event with Jev (fail closed per event: an event that
    Jev cannot label stays unclassified and never scores). With ``shadow``, Sonnet labels
    the same batch and its answers go to ``shadow``."""
    assets = sleeve_assets(cfg)
    if not assets:
        return 0
    rows = session.scalars(
        select(CatalystEvent)
        .where(CatalystEvent.classified_at.is_(None), CatalystEvent.source != "unlocks")
        .order_by(CatalystEvent.event_at.desc())
        .limit(MAX_PER_CYCLE)
    ).all()
    if not rows:
        return 0
    model = cfg.models.catalysts
    done = 0
    labelled: list[CatalystEvent] = []
    for row in rows:
        try:
            labels = await classify_with_jev(row, assets, model, cycle_id)
        except LLMError:
            continue
        row.asset = labels["asset"]
        row.type = labels["type"]
        row.direction = labels["direction"]
        row.materiality = Decimal(str(labels["materiality"]))
        row.confidence = Decimal(str(labels["type_confidence"]))
        row.classifier = labels["model"]
        row.classified_at = datetime.now(UTC)
        row.labels = labels
        labelled.append(row)
        done += 1
    session.commit()
    if shadow and labelled and cfg.catalysts.shadow_model:
        shadow_labels = await classify_with_model(
            labelled, assets, cfg.catalysts.shadow_model, cycle_id
        )
        for row in labelled:
            row.shadow = shadow_labels.get(row.id)
        session.commit()
    return done


# --- scoring (code only) ------------------------------------------------------------


def event_weight(row: CatalystEvent, now: datetime, labels: dict[str, Any] | None = None) -> float:
    """Signed contribution of one event at ``now``; 0 when it does not count."""
    lab = labels if labels is not None else _primary_labels(row)
    if not lab or not lab.get("asset"):
        return 0.0
    materiality = float(lab.get("materiality") or 0.0)
    if materiality < MIN_MATERIALITY:
        return 0.0
    kind = str(lab.get("type") or "other")
    sign = DIRECTION_SIGN.get(str(lab.get("direction") or "neutral"), 0.0)
    if row.source == "unlocks":
        decay = unlock_weight(row.event_at, now)
    else:
        age_h = max(0.0, (now - row.event_at).total_seconds() / 3600)
        if age_h > WINDOW_DAYS * 24:
            return 0.0
        decay = math.exp(-math.log(2) * age_h / HALF_LIFE_H.get(kind, 12.0))
    return sign * materiality * TYPE_WEIGHT.get(kind, 0.3) * decay


def _primary_labels(row: CatalystEvent) -> dict[str, Any]:
    return {
        "asset": row.asset,
        "type": row.type,
        "direction": row.direction,
        "materiality": float(row.materiality or 0),
    }


def score_events(
    rows: list[CatalystEvent], asset: str, now: datetime, *, shadow: bool = False
) -> tuple[float, float, list[str], list[str]]:
    """(score, confidence, evidence, risk flags) for one asset from its events. Evidence
    is built from labels only: the PMs never see event text. ``shadow`` scores the same
    rows with the shadow labels instead."""
    contributions: list[tuple[float, CatalystEvent, dict[str, Any]]] = []
    for row in rows:
        lab = (row.shadow or {}) if shadow else _primary_labels(row)
        if lab.get("asset") != asset:
            continue
        w = event_weight(row, now, lab)
        if w != 0.0:
            contributions.append((w, row, lab))
    total = sum(w for w, _, _ in contributions)
    score = max(-1.0, min(1.0, total))
    # Confidence grows with the material weight present, whichever direction it points.
    mass = sum(abs(w) for w, _, _ in contributions)
    confidence = 1 - math.exp(-mass / 0.6) if contributions else 0.0
    evidence: list[str] = []
    risk_flags: list[str] = []
    for w, row, lab in sorted(contributions, key=lambda c: -abs(c[0]))[:5]:
        when = row.event_at
        if row.source == "unlocks":
            days = (when - now).total_seconds() / 86400
            timing = f"in {days:.0f}d" if days >= 0 else f"{-days:.0f}d ago"
            evidence.append(
                f"unlock {timing}: {float(row.supply_pct or 0):.2f}% of supply ({w:+.2f})"
            )
        else:
            age_h = (now - when).total_seconds() / 3600
            src = f"@{row.author}" if row.source == "x" and row.author else row.source
            evidence.append(
                f"{lab.get('type')} {lab.get('direction')} via {src}, {age_h:.0f}h ago, "
                f"materiality {float(lab.get('materiality') or 0):.2f} ({w:+.2f})"
            )
        kind = str(lab.get("type"))
        if kind in ("hack", "delisting") and abs(w) >= 0.3:
            risk_flags.append(f"{kind} event in play ({w:+.2f})")
        elif kind == "unlock" and abs(w) >= 0.3:
            risk_flags.append(f"unlock pressure ({w:+.2f})")
    if not contributions:
        evidence = ["no material catalyst in the window"]
    return score, confidence, evidence[:10], risk_flags[:10]


def evaluate(
    session: Session,
    asset: str,
    data_age_min: int,
    now: datetime | None = None,
    *,
    shadow: bool = False,
) -> AgentOutput:
    now = now or datetime.now(UTC)
    rows = session.scalars(
        select(CatalystEvent).where(
            CatalystEvent.classified_at.is_not(None),
            CatalystEvent.event_at >= now - timedelta(days=WINDOW_DAYS),
            CatalystEvent.event_at <= now + timedelta(days=UNLOCK_RAMP_DAYS),
        )
    ).all()
    score, confidence, evidence, flags = score_events(list(rows), asset, now, shadow=shadow)
    return AgentOutput(
        agent=AGENT,
        asset=asset,
        score=score,
        confidence=confidence,
        horizon=Horizon.D1_3,
        evidence=evidence,
        risk_flags=flags,
        data_age_min=data_age_min,
    )


# --- shadow comparison --------------------------------------------------------------


@dataclass(frozen=True)
class Agreement:
    events: int
    asset: float | None
    type: float | None
    direction: float | None
    all_three: float | None


def agreement(rows: list[CatalystEvent]) -> Agreement:
    """How often Jev and the shadow model gave the same label, over events both labelled."""
    both = [r for r in rows if r.shadow and r.classified_at]
    n = len(both)
    if n == 0:
        return Agreement(0, None, None, None, None)

    def share(ok: list[bool]) -> float:
        return round(sum(ok) / n, 3)

    asset_ok = [(r.asset or None) == ((r.shadow or {}).get("asset") or None) for r in both]
    type_ok = [r.type == (r.shadow or {}).get("type") for r in both]
    dir_ok = [r.direction == (r.shadow or {}).get("direction") for r in both]
    return Agreement(
        n,
        share(asset_ok),
        share(type_ok),
        share(dir_ok),
        share([a and t and d for a, t, d in zip(asset_ok, type_ok, dir_ok, strict=True)]),
    )
