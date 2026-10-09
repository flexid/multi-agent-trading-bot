"""Indicators agent: deterministic scores from fixed rules (SPEC §6).

Everything here is pure pandas on closed candles, so the same code runs live and in the
backtest harness. The LLM only summarizes (M3); it never changes a number.

Per-indicator scores are in [-1, 1]; the agent score is their weighted mean. Rules:

- trend: close vs EMA20, EMA20 vs EMA50, EMA50 vs EMA200, each ±1/3; scaled by ADX
  (×0.5 below 20, ×1 above 25, linear between)
- macd: histogram divided by half an ATR, clipped
- rsi: momentum inside 30–70 ((rsi − 50) / 50), contrarian beyond (overbought → negative)
- obv: OBV above/below its EMA20 (±0.5), minus 0.5 when OBV and price disagree over 20 bars
- funding: crowded longs (funding above the 0.01%/8h baseline) score negative, clipped at 0.05%
- oi: 24h open-interest change confirms the 24h price direction, clipped at ±5%

Confidence starts at 0.5, +0.2 when ADX > 25, +0.2 × agreement between indicators,
−0.2 when ATR is above its 30-day 90th percentile (also a risk flag).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pandas as pd
import pandas_ta as ta

from app.agents.schema import AgentOutput, Horizon

AGENT = "indicators"
BARS_PER_DAY = {"15": 96, "60": 24, "240": 6, "D": 1}
MIN_BARS = 210  # EMA 200 needs warm-up

WEIGHTS = {"trend": 0.30, "macd": 0.20, "rsi": 0.15, "obv": 0.15, "funding": 0.10, "oi": 0.10}
FUNDING_BASELINE = 0.0001  # 0.01% per 8h is the exchange default
FUNDING_SCALE = 0.0005
OI_SCALE = 0.05
ATR_PCTL_WINDOW_DAYS = 30
ATR_PCTL = 0.90


@dataclass(frozen=True)
class PerpInputs:
    funding_rate: float
    oi_change_24h: float  # fraction, e.g. 0.03 for +3%


def _clip(x: float) -> float:
    return float(np.clip(x, -1.0, 1.0))


def features(candles: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Add indicator columns. ``candles`` has open, high, low, close, volume, UTC index."""
    df = candles.copy()
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    df["ema20"] = ta.ema(close, length=20)
    df["ema50"] = ta.ema(close, length=50)
    df["ema200"] = ta.ema(close, length=200)
    adx = ta.adx(high, low, close, length=14)
    df["adx"] = adx["ADX_14"] if adx is not None else np.nan
    df["rsi"] = ta.rsi(close, length=14)
    macd = ta.macd(close, fast=12, slow=26, signal=9)
    df["macd_hist"] = macd["MACDh_12_26_9"] if macd is not None else np.nan
    df["atr"] = ta.atr(high, low, close, length=14)
    per_day = BARS_PER_DAY[interval]
    df["rvol"] = np.log(close).diff().rolling(14).std() * np.sqrt(per_day * 365)
    df["obv"] = ta.obv(close, vol)
    df["obv_ema20"] = ta.ema(df["obv"], length=20)
    window = ATR_PCTL_WINDOW_DAYS * per_day
    df["atr_p90"] = df["atr"].rolling(window, min_periods=window // 2).quantile(ATR_PCTL)
    return df


def score_row(df: pd.DataFrame, i: int, perp: PerpInputs | None = None) -> dict[str, float]:
    """Per-indicator scores at bar ``i`` using only bars ≤ i."""
    row = df.iloc[i]
    out: dict[str, float] = {}

    adx = float(row["adx"])
    adx_scale = float(np.clip((adx - 20) / 5, 0, 1)) * 0.5 + 0.5 if np.isfinite(adx) else 0.5
    trend = (
        np.sign(row["close"] - row["ema20"])
        + np.sign(row["ema20"] - row["ema50"])
        + np.sign(row["ema50"] - row["ema200"])
    ) / 3
    out["trend"] = _clip(float(trend) * adx_scale)

    atr = float(row["atr"])
    out["macd"] = _clip(float(row["macd_hist"]) / (0.5 * atr)) if atr > 0 else 0.0

    rsi = float(row["rsi"])
    if rsi >= 70:
        out["rsi"] = _clip(-(rsi - 70) / 30 - 0.2)
    elif rsi <= 30:
        out["rsi"] = _clip((30 - rsi) / 30 + 0.2)
    else:
        out["rsi"] = (rsi - 50) / 50

    obv = 0.5 if row["obv"] > row["obv_ema20"] else -0.5
    if i >= 20:
        price_up = row["close"] > df["close"].iloc[i - 20]
        obv_up = row["obv"] > df["obv"].iloc[i - 20]
        if price_up != obv_up:
            obv -= 0.5 if price_up else -0.5  # divergence works against the price move
    out["obv"] = _clip(obv)

    if perp is not None:
        out["funding"] = _clip(-(perp.funding_rate - FUNDING_BASELINE) / FUNDING_SCALE)
        price_24h = BARS_PER_DAY[_interval_of(df)]
        if i >= price_24h:
            direction = np.sign(row["close"] - df["close"].iloc[i - price_24h])
            out["oi"] = _clip(float(direction) * perp.oi_change_24h / OI_SCALE)
        else:
            out["oi"] = 0.0
    return out


def _interval_of(df: pd.DataFrame) -> str:
    return str(df.attrs.get("interval", "240"))


def combine(scores: dict[str, float]) -> tuple[float, float]:
    """Weighted score and the agreement (share of components on the majority side)."""
    weights = {k: WEIGHTS[k] for k in scores}
    total = sum(weights.values())
    score = sum(scores[k] * w for k, w in weights.items()) / total
    signs = [np.sign(v) for v in scores.values() if v != 0]
    agreement = abs(sum(signs)) / len(signs) if signs else 0.0
    return _clip(score), float(agreement)


def score_series(df: pd.DataFrame) -> pd.Series:
    """Agent score at every bar, candle-only components (for backtests)."""
    values = np.full(len(df), np.nan)
    for i in range(MIN_BARS, len(df)):
        values[i] = combine(score_row(df, i))[0]
    return pd.Series(values, index=df.index, name="score")


def evaluate(
    asset: str,
    candles: pd.DataFrame,
    interval: str,
    perp: PerpInputs | None,
    data_age_min: int,
) -> AgentOutput:
    df = features(candles, interval)
    df.attrs["interval"] = interval
    if len(df) < MIN_BARS:
        raise ValueError(f"{asset}: need {MIN_BARS} bars, have {len(df)}")
    i = len(df) - 1
    parts = score_row(df, i, perp)
    score, agreement = combine(parts)
    row = df.iloc[i]

    vol_extreme = bool(np.isfinite(row["atr_p90"]) and row["atr"] > row["atr_p90"])
    confidence = (
        0.5 + (0.2 if row["adx"] > 25 else 0.0) + 0.2 * agreement - (0.2 if vol_extreme else 0.0)
    )

    evidence = [
        f"trend {parts['trend']:+.2f}: close {row['close']:.6g} vs EMA20 {row['ema20']:.6g}, "
        f"EMA50 {row['ema50']:.6g}, EMA200 {row['ema200']:.6g}, ADX {row['adx']:.0f}",
        f"MACD hist {row['macd_hist']:.4g} vs ATR {row['atr']:.4g} → {parts['macd']:+.2f}",
        f"RSI {row['rsi']:.0f} → {parts['rsi']:+.2f}",
        f"OBV {'above' if row['obv'] > row['obv_ema20'] else 'below'} its EMA20 "
        f"→ {parts['obv']:+.2f}",
        f"realized vol {row['rvol'] * 100:.0f}% annualized",
    ]
    if perp is not None:
        evidence.append(
            f"funding {perp.funding_rate * 100:.4f}%/8h → {parts['funding']:+.2f}, "
            f"OI 24h {perp.oi_change_24h * 100:+.1f}% → {parts['oi']:+.2f}"
        )
    risk_flags = ["ATR above 30-day 90th percentile"] if vol_extreme else []
    return AgentOutput(
        agent=AGENT,
        asset=asset,
        score=score,
        confidence=float(np.clip(confidence, 0, 1)),
        horizon=Horizon.D1_3,
        evidence=evidence,
        risk_flags=risk_flags,
        data_age_min=data_age_min,
    )


def candles_frame(
    rows: list[tuple[object, Decimal, Decimal, Decimal, Decimal, Decimal]],
) -> pd.DataFrame:
    """Build the OHLCV frame from (open_time, open, high, low, close, volume) rows."""
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.set_index("open_time").sort_index()
    return df.astype(float)
