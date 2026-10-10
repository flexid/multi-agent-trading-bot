"""Macro agent (SPEC §6).

Code side:
- normalizes each macro series into a z-score of its 5-day change against 90 days
- lists scheduled events (FOMC, CPI, jobs) in the next 48 hours from the calendar file
- computes per-asset coupling in [0, 1]: the mean of the positive parts of the rolling
  7-day and 30-day correlations between the asset's daily returns and S&P 500, Nasdaq,
  the dollar index (sign flipped: a strong dollar is risk-off) and gold. Negative
  correlation is decoupled, never an inverted signal.
Model side (gpt-5.6-terra): regime and 48h event risk only, from the normalized JSON.

Output has two logged parts (``components``):
- tradfi = regime × confidence × coupling. Low coupling pushes it to zero.
- native = Fear & Greed (contrarian only at extremes: above 80 leans against longs, below
  20 against shorts, near zero in between) + BTC dominance tilt (7-day change in
  percentage points: rising favours BTC and weighs on alts, strongest on memecoins;
  falling is the reverse). Not scaled by coupling.
- score = clip(tradfi + native). Extreme Fear & Greed is also a risk flag for the
  leverage agent (SPEC §8).
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.schema import AgentOutput, Horizon
from app.config import get_config
from app.db.models import Candle, MacroObservation, PolymarketMarket, PolymarketPrice
from app.llm import LLMError, Prompt, complete

AGENT = "macro"
TASK = "macro"
CALENDAR = Path(__file__).resolve().parent.parent / "data" / "calendar_2026.toml"
SERIES = ["DGS2", "DGS10", "DTWEXBGS", "VIXCLS", "SP500", "NASDAQCOM", "XAUUSD", "STABLES_USD"]
FNG_HIGH, FNG_LOW = 80.0, 20.0
DOMINANCE_SCALE_PP = 3.0  # a 3-point 7-day move in BTC dominance is a full tilt
DOMINANCE_TILT = {
    "BTC": 1.0,
    "ETH": -1.0,
    "SOL": -1.0,
    "XRP": -1.0,
    "DOGE": -1.5,
    "PEPE": -1.5,
    "HBAR": -1.0,
    "PUMP": -1.5,
    "SPX6900": -1.5,
    "ENA": -1.0,
    "BNB": -1.0,
}
TRADFI = {"SP500": 1.0, "NASDAQCOM": 1.0, "DTWEXBGS": -1.0, "XAUUSD": 1.0}
CHANGE_DAYS = 5
HISTORY_DAYS = 90
REGIME_SCORE = {"risk_on": 1.0, "neutral": 0.0, "risk_off": -1.0}


class Regime(StrEnum):
    RISK_ON = "risk_on"
    NEUTRAL = "neutral"
    RISK_OFF = "risk_off"


class MacroView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regime: Regime
    regime_confidence: float = Field(ge=0, le=1)
    event_risk_48h: float = Field(ge=0, le=1)
    reasons: list[str] = Field(max_length=3)


@dataclass(frozen=True)
class MacroInputs:
    z_changes: dict[str, float]  # series -> z-score of the 5-day change
    events_48h: list[str]
    fed_odds: dict[str, float]  # market question stem -> P(yes)
    as_of: datetime
    fng: float | None = None
    dominance_change_7d: float | None = None


# --- code: normalization, calendar, coupling ----------------------------------


def series_frame(session: Session, since: date) -> pd.DataFrame:
    rows = session.execute(
        select(MacroObservation.series, MacroObservation.date, MacroObservation.value).where(
            MacroObservation.date >= datetime.combine(since, datetime.min.time(), tzinfo=UTC)
        )
    ).all()
    df = pd.DataFrame(rows, columns=["series", "date", "value"])
    if df.empty:
        return df
    df["value"] = df["value"].astype(float)
    wide = df.pivot(index="date", columns="series", values="value").sort_index()
    return wide.ffill()


def z_changes(wide: pd.DataFrame) -> dict[str, float]:
    out: dict[str, float] = {}
    for name in SERIES:
        if name not in wide:
            continue
        s = wide[name].dropna()
        if len(s) < CHANGE_DAYS * 3:
            continue
        change = s.pct_change(CHANGE_DAYS) if name not in ("DGS2", "DGS10") else s.diff(CHANGE_DAYS)
        change = change.dropna()
        std = float(change.std())
        if std == 0 or np.isnan(std):
            continue
        out[name] = round(float((change.iloc[-1] - change.mean()) / std), 2)
    return out


def events_within(hours: int, now: datetime, calendar: Path = CALENDAR) -> list[str]:
    with calendar.open("rb") as fh:
        data = tomllib.load(fh)
    horizon = now + timedelta(hours=hours)
    out = []
    for block in data["events"]:
        for d in block["dates"]:
            day = date.fromisoformat(d)
            start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
            if start <= horizon and start + timedelta(days=1) > now:
                out.append(f"{block['kind']} {d}")
    return out


def event_today(now: datetime, calendar: Path = CALENDAR) -> bool:
    """FOMC, CPI or jobs report today (SPEC §8 leverage cap)."""
    return any(e.endswith(now.date().isoformat()) for e in events_within(0, now, calendar))


def fed_odds(session: Session, now: datetime) -> dict[str, float]:
    markets = session.scalars(
        select(PolymarketMarket).where(
            PolymarketMarket.closed.is_(False),
            PolymarketMarket.slug.ilike("%fed%interest%rates%"),
            PolymarketMarket.end_date > now,
        )
    ).all()
    out: dict[str, float] = {}
    for m in sorted(markets, key=lambda m: m.end_date or now)[:6]:
        row = session.execute(
            select(PolymarketPrice.prices)
            .where(PolymarketPrice.market_id == m.id)
            .order_by(PolymarketPrice.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row:
            yes = next((i for i, o in enumerate(m.outcomes) if str(o).lower() == "yes"), 0)
            out[m.slug[:60]] = round(float(row[yes]), 3)
    return out


def coupling(asset_closes: pd.Series, wide: pd.DataFrame) -> float:
    """Mean positive correlation of daily returns with tradfi over 7 and 30 days."""
    rets = asset_closes.pct_change().dropna()
    scores: list[float] = []
    for name, sign in TRADFI.items():
        if name not in wide:
            continue
        tradfi = wide[name].pct_change().dropna() * sign
        joined = pd.concat([rets, tradfi], axis=1, join="inner").dropna()
        for window in (7, 30):
            if len(joined) < window:
                continue
            corr = float(joined.iloc[-window:].corr().to_numpy()[0, 1])
            scores.append(max(0.0, corr) if np.isfinite(corr) else 0.0)
    return round(float(np.mean(scores)), 2) if scores else 0.0


def asset_daily_closes(session: Session, symbol: str, days: int) -> pd.Series:
    rows = session.execute(
        select(Candle.open_time, Candle.close)
        .where(
            Candle.symbol == symbol,
            Candle.interval == "D",
            Candle.open_time >= datetime.now(UTC) - timedelta(days=days),
        )
        .order_by(Candle.open_time)
    ).all()
    s = pd.Series([float(r.close) for r in rows], index=[r.open_time.date() for r in rows])
    s.index = pd.to_datetime(s.index, utc=True)
    return s


def build_inputs(session: Session, now: datetime) -> tuple[MacroInputs, pd.DataFrame]:
    wide = series_frame(session, (now - timedelta(days=HISTORY_DAYS)).date())
    return (
        MacroInputs(
            z_changes=z_changes(wide),
            events_48h=events_within(48, now),
            fed_odds=fed_odds(session, now),
            as_of=now,
            fng=latest_fng(session),
            dominance_change_7d=dominance_change_7d(session, now),
        ),
        wide,
    )


# --- crypto-native inputs ---------------------------------------------------------


def fng_score(value: float | None) -> float:
    """Contrarian only at extremes; near-neutral in between."""
    if value is None:
        return 0.0
    if value >= FNG_HIGH:
        return -min(1.0, (value - FNG_HIGH) / (100 - FNG_HIGH))
    if value <= FNG_LOW:
        return min(1.0, (FNG_LOW - value) / FNG_LOW)
    return -0.1 * (value - 50) / 30  # ±0.1 at the edges of the neutral band


def dominance_tilt(asset: str, change_7d_pp: float | None) -> float:
    if change_7d_pp is None:
        return 0.0
    strength = max(-1.0, min(1.0, change_7d_pp / DOMINANCE_SCALE_PP))
    return max(-1.0, min(1.0, strength * DOMINANCE_TILT.get(asset, -1.0)))


def latest_fng(session: Session) -> float | None:
    row = session.execute(
        select(MacroObservation.value)
        .where(MacroObservation.series == "FNG")
        .order_by(MacroObservation.date.desc())
        .limit(1)
    ).scalar_one_or_none()
    return float(row) if row is not None else None


def dominance_change_7d(session: Session, now: datetime) -> float | None:
    """BTC dominance now minus 7 days ago, in percentage points. Falls back to the ETHBTC
    proxy (inverted, scaled) while dominance history is shorter than 7 days."""
    rows = session.execute(
        select(MacroObservation.date, MacroObservation.value)
        .where(MacroObservation.series == "BTC_DOMINANCE")
        .order_by(MacroObservation.date.desc())
    ).all()
    if rows and rows[-1].date <= now - timedelta(days=7):
        latest = float(rows[0].value)
        older = next(float(v) for d, v in rows if d <= now - timedelta(days=7))
        return latest - older
    closes = asset_daily_closes(session, "ETHBTC", 12)
    if len(closes) >= 8:
        return float(-(closes.iloc[-1] / closes.iloc[-8] - 1) * 100 * 0.6)  # ~ETHBTC −5% ≈ +3 pp
    return None


# --- model: regime -------------------------------------------------------------


async def read_regime(
    inputs: MacroInputs, prompt: Prompt, cycle_id: int | None = None
) -> MacroView:
    payload = {
        "as_of": inputs.as_of.isoformat(timespec="minutes"),
        "z_score_of_5d_change": inputs.z_changes,
        "scheduled_events_48h": inputs.events_48h,
        "fed_decision_odds": inputs.fed_odds,
        "fear_greed_index": inputs.fng,
        "btc_dominance_change_7d_pp": inputs.dominance_change_7d,
    }
    result = await complete(
        TASK, MacroView, prompt=prompt, user_text=json.dumps(payload), cycle_id=cycle_id
    )
    return result.parsed


# --- output ------------------------------------------------------------------------


def to_output(
    asset: str,
    view: MacroView | None,
    asset_coupling: float,
    inputs: MacroInputs,
    data_age_min: int,
) -> AgentOutput:
    if view is None:
        raise LLMError("macro: no regime")
    tradfi = REGIME_SCORE[view.regime.value] * view.regime_confidence * asset_coupling
    fng = fng_score(inputs.fng)
    dom = dominance_tilt(asset, inputs.dominance_change_7d)
    weights = get_config().agents.macro
    native = max(-1.0, min(1.0, weights.fng_weight * fng + weights.dominance_weight * dom))
    score = max(-1.0, min(1.0, tradfi + native))
    risk_flags = []
    if inputs.events_48h:
        risk_flags.append("scheduled: " + ", ".join(inputs.events_48h))
    if view.event_risk_48h >= 0.6:
        risk_flags.append(f"event risk 48h {view.event_risk_48h:.1f}")
    if inputs.fng is not None and (inputs.fng >= FNG_HIGH or inputs.fng <= FNG_LOW):
        risk_flags.append(f"fear & greed extreme ({inputs.fng:.0f})")
    fng_txt = f"{inputs.fng:.0f}" if inputs.fng is not None else "n/a"
    dom_txt = (
        f"{inputs.dominance_change_7d:+.1f} pp" if inputs.dominance_change_7d is not None else "n/a"
    )
    evidence = [
        f"regime {view.regime.value} (conf {view.regime_confidence:.2f}), "
        f"coupling {asset_coupling:.2f}",
        f"tradfi {tradfi:+.2f} | native {native:+.2f}: fear&greed {fng_txt} → {fng:+.2f}, "
        f"BTC dominance 7d {dom_txt} → {dom:+.2f}",
        *view.reasons,
        "z(5d): " + ", ".join(f"{k} {v:+.1f}" for k, v in inputs.z_changes.items()),
    ]
    confidence = view.regime_confidence * (0.5 + 0.5 * asset_coupling)
    confidence = max(confidence, 0.4 * abs(native))  # native inputs need no coupling
    return AgentOutput(
        agent=AGENT,
        asset=asset,
        score=max(-1.0, min(1.0, score)),
        confidence=max(0.0, min(1.0, confidence)),
        horizon=Horizon.D1_3,
        evidence=evidence[:10],
        risk_flags=risk_flags,
        data_age_min=data_age_min,
        components={
            "tradfi": round(tradfi, 4),
            "native": round(native, 4),
            "fng": round(fng, 4),
            "dominance": round(dom, 4),
        },
    )
