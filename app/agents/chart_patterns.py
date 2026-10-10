"""Chart patterns agent (SPEC §6): two tracks, full weight only when they agree.

Code track (deterministic, per timeframe 1h / 4h / 1D):
- swing highs and lows (fractals over ±3 bars), clustered into support/resistance levels
- the most recent range: last 30 bars between the nearest level above and below
- breakout: close beyond a level within the last 3 bars, confirmed by volume above its
  20-bar mean; false breakout when price is back inside within the same window
- trendline: slope of a regression through the last 20 swing lows (uptrend) or highs
  (downtrend) relative to ATR
- score per timeframe from breakout direction, trend slope and position in the range;
  the 4h timeframe weighs 0.5, 1D 0.3, 1h 0.2

Vision track: mplfinance renders the same three charts in a fixed style (no indicators
except EMA 20/50, volume panel, 120 bars) and ``claude-opus-5-5`` returns a schema:
pattern, direction, confidence, key levels. It never sees the code track's answer.

Combination: agree on direction → score = mean of both, confidence up; disagree → score
halved toward the code track and confidence down (SPEC: "full weight only when both
tracks agree").
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from app.agents.schema import AgentOutput, Horizon
from app.config import get_config
from app.llm import Image, LLMError, Prompt, complete

AGENT = "chart_patterns"
TASK = "chart_patterns"
TIMEFRAMES = {"60": 0.2, "240": 0.5, "D": 0.3}
TF_LABEL = {"60": "1h", "240": "4h", "D": "1D"}
FRACTAL = 3
RANGE_BARS = 30
BREAKOUT_BARS = 3
LEVEL_TOL = 0.004  # cluster swings within 0.4% into one level
RENDER_BARS = 120


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"
    NONE = "none"


class VisionRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(max_length=60)  # e.g. "descending triangle", "range", "none"
    direction: Direction
    confidence: float = Field(ge=0, le=1)
    key_levels: list[float] = Field(max_length=4)
    note: str = Field(max_length=200)


@dataclass(frozen=True)
class CodeRead:
    timeframe: str
    score: float  # [-1, 1]
    levels_above: list[float]
    levels_below: list[float]
    breakout: Direction
    false_breakout: bool
    trend_slope_atr: float  # per bar, in ATR units
    range_position: float  # 0 bottom .. 1 top of the current range


# --- code track ---------------------------------------------------------------------


def swings(
    df: pd.DataFrame, k: int = FRACTAL
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    sh, sl = [], []
    for i in range(k, len(df) - k):
        if highs[i] == highs[i - k : i + k + 1].max():
            sh.append((i, float(highs[i])))
        if lows[i] == lows[i - k : i + k + 1].min():
            sl.append((i, float(lows[i])))
    return sh, sl


def cluster_levels(prices: list[float], tol: float = LEVEL_TOL) -> list[float]:
    out: list[list[float]] = []
    for p in sorted(prices):
        if out and abs(p - out[-1][-1]) / p <= tol:
            out[-1].append(p)
        else:
            out.append([p])
    return [float(np.mean(group)) for group in out if len(group) >= 2]


def _slope_atr(points: list[tuple[int, float]], atr: float) -> float:
    if len(points) < 3 or atr <= 0:
        return 0.0
    xs = np.array([p[0] for p in points[-20:]], dtype=float)
    ys = np.array([p[1] for p in points[-20:]], dtype=float)
    slope = float(np.polyfit(xs, ys, 1)[0])
    return slope / atr


def code_track(df: pd.DataFrame, timeframe: str) -> CodeRead:
    close = float(df["close"].iloc[-1])
    tr = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            (df["high"] - df["close"].shift()).abs(), (df["low"] - df["close"].shift()).abs()
        ),
    )
    atr = float(tr.rolling(14).mean().iloc[-1]) or 1e-9
    sh, sl = swings(df)
    levels = cluster_levels([p for _, p in sh] + [p for _, p in sl])
    above = sorted(lv for lv in levels if lv > close)[:3]
    below = sorted((lv for lv in levels if lv < close), reverse=True)[:3]

    recent = df.iloc[-RANGE_BARS:]
    lo, hi = float(recent["low"].min()), float(recent["high"].max())
    range_pos = (close - lo) / (hi - lo) if hi > lo else 0.5

    vol_ok = bool(
        df["volume"].iloc[-BREAKOUT_BARS:].max() > df["volume"].rolling(20).mean().iloc[-1]
    )
    prior_hi = float(df["high"].iloc[-RANGE_BARS:-BREAKOUT_BARS].max())
    prior_lo = float(df["low"].iloc[-RANGE_BARS:-BREAKOUT_BARS].min())
    last = df["close"].iloc[-BREAKOUT_BARS:]
    breakout, false_bo = Direction.NONE, False
    if (last > prior_hi).any():
        breakout = Direction.UP if vol_ok and close > prior_hi else Direction.NONE
        false_bo = close <= prior_hi
    elif (last < prior_lo).any():
        breakout = Direction.DOWN if vol_ok and close < prior_lo else Direction.NONE
        false_bo = close >= prior_lo

    slope = _slope_atr(sl, atr) if close > (sl[-1][1] if sl else close) else _slope_atr(sh, atr)
    slope = float(np.clip(slope, -1, 1))

    score = 0.0
    if breakout is Direction.UP:
        score += 0.6
    elif breakout is Direction.DOWN:
        score -= 0.6
    if false_bo:
        score -= (
            0.4 if (last > prior_hi).any() else -0.4
        )  # failed up-break is bearish, and vice versa
    score += 0.3 * slope
    score += 0.2 * (range_pos - 0.5) * 2 * (1 if slope >= 0 else -1) * 0.5
    return CodeRead(
        timeframe=timeframe,
        score=float(np.clip(score, -1, 1)),
        levels_above=above,
        levels_below=below,
        breakout=breakout,
        false_breakout=false_bo,
        trend_slope_atr=slope,
        range_position=round(range_pos, 2),
    )


# --- vision track -------------------------------------------------------------------


def render(df: pd.DataFrame, title: str) -> Image:
    """Fixed-style candlestick PNG: last 120 bars, EMA 20/50, volume, no annotations."""
    import matplotlib

    matplotlib.use("Agg")
    import mplfinance as mpf

    data = df.iloc[-RENDER_BARS:].copy()
    data.index.name = "Date"
    data = data.rename(
        columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
    )
    ema20 = data["Close"].ewm(span=20).mean()
    ema50 = data["Close"].ewm(span=50).mean()
    buf = io.BytesIO()
    mpf.plot(
        data,
        type="candle",
        style="yahoo",
        volume=True,
        addplot=[
            mpf.make_addplot(ema20, color="#1f77b4", width=0.8),
            mpf.make_addplot(ema50, color="#ff7f0e", width=0.8),
        ],
        title=title,
        figsize=(10, 6),
        savefig={"fname": buf, "dpi": 110, "format": "png"},
    )
    return Image(media_type="image/png", data_b64=base64.b64encode(buf.getvalue()).decode())


async def vision_track(
    asset: str, images: dict[str, Image], prompt: Prompt, cycle_id: int | None = None
) -> dict[str, VisionRead]:
    out: dict[str, VisionRead] = {}
    for tf, image in images.items():
        try:
            sleeve = get_config().sleeve_cfg(asset)
            result = await complete(
                TASK,
                VisionRead,
                prompt=prompt,
                user_text=json.dumps({"asset": asset, "timeframe": TF_LABEL[tf]}),
                images=[image],
                cycle_id=cycle_id,
                asset=asset,
                model=sleeve.models.get(TASK) if sleeve else None,  # cheaper vision for alts
            )
        except LLMError:
            continue
        out[tf] = result.parsed
    return out


# --- combination -------------------------------------------------------------------


def combine(
    asset: str, code: dict[str, CodeRead], vision: dict[str, VisionRead], data_age_min: int
) -> AgentOutput:
    score_parts, conf_parts, evidence, flags = [], [], [], []
    for tf, weight in TIMEFRAMES.items():
        c = code.get(tf)
        if c is None:
            continue
        v = vision.get(tf)
        if v is None:
            score_parts.append(weight * c.score * 0.5)  # code only: half weight
            conf_parts.append(weight * 0.3)
            evidence.append(f"{TF_LABEL[tf]} code {c.score:+.2f} (no vision read)")
            continue
        v_score = {"up": 1.0, "down": -1.0, "none": 0.0}[v.direction.value] * v.confidence
        agree = np.sign(c.score) == np.sign(v_score) and c.score != 0 and v_score != 0
        if agree:
            score_parts.append(weight * (c.score + v_score) / 2)
            conf_parts.append(weight * (0.6 + 0.4 * v.confidence))
        else:
            score_parts.append(weight * c.score * 0.25)  # disagreement: less than no read
            conf_parts.append(weight * 0.2)
        evidence.append(
            f"{TF_LABEL[tf]}: code {c.score:+.2f} ({c.breakout.value} breakout"
            f"{', false' if c.false_breakout else ''}, slope {c.trend_slope_atr:+.2f} ATR, "
            f"range pos {c.range_position:.2f}); vision {v.pattern} {v.direction.value} "
            f"{v.confidence:.2f}{'' if agree else ' (disagree)'}"
        )
    main = code.get("240") or next(iter(code.values()), None)
    if main is not None:
        evidence.append(
            "levels above "
            + ", ".join(f"{x:g}" for x in main.levels_above)
            + "; below "
            + ", ".join(f"{x:g}" for x in main.levels_below)
        )
        if main.false_breakout:
            flags.append("false breakout on 4h")
    return AgentOutput(
        agent=AGENT,
        asset=asset,
        score=float(np.clip(sum(score_parts), -1, 1)),
        confidence=float(np.clip(sum(conf_parts), 0, 1)),
        horizon=Horizon.D1_3,
        evidence=evidence[:10],
        risk_flags=flags,
        data_age_min=data_age_min,
    )
