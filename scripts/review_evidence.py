"""The evidence for the November 2026 review (docs/RETHINK_2026-11.md).

Part A asks what the market itself does, on the candle history in the database (daily
since January 2024, 4-hour since October 2025 or April 2026): does a move continue or
revert, at which horizon, and what do a handful of textbook rules earn after fees.
Part B asks what each part of the bot predicts, on everything it logged since the record
line: the calls against the drift, the agents' scores at four horizons, the indicators
score recomputed on the long history, the trades and their costs.

Read-only. Few, pre-declared variants; every one is printed, the bad ones too.

python -m scripts.review_evidence [a|b|c]
"""

# ruff: noqa: E501

from __future__ import annotations

import sys
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.db.session import new_session

ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ENA"]
SYMS = [f"{a}USDT" for a in ASSETS]
FEE = 0.001  # spot taker, per side, of notional
BORROW_DAY = 0.04 / 365  # what a short pays to borrow the coin, about 4% a year
RECORD = pd.Timestamp("2026-10-12 19:33", tz="UTC")
HOURS = {"4h": 4, "1d": 24, "3d": 72, "7d": 168}
pd.set_option("display.width", 220)


def q(sql: str, **params: Any) -> pd.DataFrame:
    with new_session() as s:
        res = s.execute(text(sql), params)
        return pd.DataFrame(res.all(), columns=list(res.keys()))


def candles(interval: str) -> dict[str, pd.DataFrame]:
    df = q(
        "select symbol, open_time t, open o, high h, low l, close c, volume v from candles "
        "where interval = :i and symbol = any(:s) order by open_time",
        i=interval,
        s=SYMS,
    )
    df["t"] = pd.to_datetime(df["t"], utc=True)
    for col in "ohlcv":
        df[col] = df[col].astype(float)
    out = {}
    for a in ASSETS:
        d = df[df.symbol == f"{a}USDT"].set_index("t").sort_index()
        out[a] = d.iloc[:-1]  # the last candle may still be running
    return out


def spearman(a: pd.Series, b: pd.Series) -> float:
    m = a.notna() & b.notna()
    if m.sum() < 20:
        return float("nan")
    return float(a[m].rank().corr(b[m].rank()))


def stats(ret: pd.Series, per_year: float) -> dict[str, float]:
    ret = ret.dropna()
    if ret.empty:
        return {}
    eq = (1 + ret).cumprod()
    vol = ret.std() * np.sqrt(per_year)
    return {
        "ann%": ret.mean() * per_year * 100,
        "cagr%": (eq.iloc[-1] ** (per_year / len(ret)) - 1) * 100,
        "vol%": vol * 100,
        "sharpe": ret.mean() * per_year / vol if vol > 0 else float("nan"),
        "maxDD%": (eq / eq.cummax() - 1).min() * 100,
        "total%": (eq.iloc[-1] - 1) * 100,
    }


# ----------------------------------------------------------------------------- Part A


def daily_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    d = candles("D")
    close = pd.DataFrame({a: d[a]["c"] for a in ASSETS})
    return close, close.pct_change()


def part_a_structure(close: pd.DataFrame, r: pd.DataFrame) -> None:
    print("\n=== A1. Does a daily move continue or revert? (pooled over six assets)")
    sigma = r.rolling(20).std().shift(1)
    z = r / sigma
    nxt = r.shift(-1)
    rows = []
    periods = {
        "2024": ("2024-01-01", "2024-12-31"),
        "2025": ("2025-01-01", "2025-12-31"),
        "2026 to 11 Oct": ("2026-01-01", "2026-10-11"),
        "bot's life (12 Oct on)": ("2026-10-12", "2030-01-01"),
        "all": ("2024-01-01", "2030-01-01"),
    }
    for name, (a, b) in periods.items():
        sl = slice(a, b)
        cont = (np.sign(r) * nxt)[sl]
        big = (z.abs() > 1)[sl]
        up = ((z > 1)[sl], nxt[sl])
        dn = ((z < -1)[sl], nxt[sl])
        vals = cont[big].stack().dropna()
        rows.append(
            {
                "period": name,
                "n big days": int(big.sum().sum()),
                "next day, in the move's direction %": vals.mean() * 100,
                "t (n/3 independent)": vals.mean() / vals.std() * np.sqrt(len(vals) / 3)
                if len(vals) > 5
                else np.nan,
                "after +1σ day: next %": up[1][up[0]].stack().mean() * 100,
                "after -1σ day: next %": dn[1][dn[0]].stack().mean() * 100,
                "lag-1 autocorr": float(np.nanmean([r[c][sl].autocorr(1) for c in r.columns])),
            }
        )
    print(pd.DataFrame(rows).round(3).to_string(index=False))

    print("\n=== A1b. Does a multi-week trend continue? sign(28-day return) x next 7 days, pooled")
    trend = np.sign(close / close.shift(28) - 1)
    fwd7 = close.shift(-7) / close - 1
    rows = []
    for name, (a, b) in periods.items():
        sl = slice(a, b)
        v = (trend * fwd7)[sl].stack().dropna()
        rows.append(
            {
                "period": name,
                "n": len(v),
                "next 7d in the trend's direction %": v.mean() * 100,
                "share right": (v > 0).mean(),
            }
        )
    print(pd.DataFrame(rows).round(3).to_string(index=False))


def positions(close: pd.DataFrame, r: pd.DataFrame) -> dict[str, pd.DataFrame]:
    sigma = r.rolling(20).std().shift(1)
    z = r / sigma
    trend = np.sign(close / close.shift(28) - 1)
    out: dict[str, pd.DataFrame] = {}
    out["hold (long all six)"] = close.notna().astype(float)
    out["trend 28d, long/short"] = trend
    out["trend 28d, long or cash"] = trend.clip(lower=0)
    out["trend 28d, short or cash"] = trend.clip(upper=0)
    out["chase: follow a >1σ day, 1 day"] = np.sign(r).where(z.abs() > 1, 0.0)
    out["fade: against a >1σ day, 1 day"] = -np.sign(r).where(z.abs() > 1, 0.0)
    # with the trend, entered on a counter-trend day, held three days or until the trend turns
    tp = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for a in close.columns:
        left, side = 0, 0.0
        for i in range(len(close)):
            t = trend[a].iloc[i]
            if not np.isfinite(t) or t == 0:
                left, side = 0, 0.0
            else:
                zz, rr = z[a].iloc[i], r[a].iloc[i]
                if np.isfinite(zz) and abs(zz) > 0.5 and np.sign(rr) == -t:
                    left, side = 3, float(t)
                if side != t:
                    left, side = 0, 0.0
            tp.iloc[i, tp.columns.get_loc(a)] = side if left > 0 else 0.0
            left = max(0, left - 1)
    out["with the trend, on a pullback, 3 days"] = tp
    # cross-sectional: long the two strongest, short the two weakest, weekly
    mom = close / close.shift(28) - 1
    xs = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    last = None
    for i, day in enumerate(close.index):
        if i % 7 == 0:
            row = mom.loc[day].dropna()
            last = pd.Series(0.0, index=close.columns)
            if len(row) >= 5:
                order = row.sort_values()
                last[order.index[:2]] = -1.0
                last[order.index[-2:]] = 1.0
        if last is not None:
            xs.loc[day] = last
    out["relative: long 2 strongest, short 2 weakest, weekly"] = xs
    return out


def run(pos: pd.DataFrame, r: pd.DataFrame, fee: float = FEE) -> tuple[pd.Series, float]:
    """Portfolio daily return (equal weight over the assets with data) and turnover a year."""
    p = pos.shift(1)  # decided at yesterday's close, earned today
    trade = (pos - pos.shift(1)).abs().shift(1)
    net = p * r - trade.fillna(0) * fee - (p < 0) * BORROW_DAY
    net = net.where(r.notna())
    port = net.mean(axis=1)
    years = len(r) / 365
    turnover = (
        float(trade.where(r.notna()).sum().sum() / r.notna().sum().sum() * 365) if years else 0.0
    )
    return port, turnover


def part_a_strategies(close: pd.DataFrame, r: pd.DataFrame) -> None:
    print(
        "\n=== A2. Textbook rules on daily bars, six assets, equal weight, 1x, after 0.1% a side and borrow"
    )
    rows = []
    for name, pos in positions(close, r).items():
        port, turn = run(pos, r)
        port = port["2024-03-01":]
        st = stats(port, 365)
        by_year = {str(y): ((1 + g).prod() - 1) * 100 for y, g in port.groupby(port.index.year)}
        bot = ((1 + port[RECORD.normalize() :]).prod() - 1) * 100
        last90 = ((1 + port.iloc[-90:]).prod() - 1) * 100
        oos = stats(port["2025-07-01":], 365)
        rows.append(
            {
                "rule": name,
                "cagr%": st["cagr%"],
                "sharpe": st["sharpe"],
                "maxDD%": st["maxDD%"],
                **by_year,
                "sharpe since Jul 25": oos.get("sharpe", np.nan),
                "last 90d %": last90,
                "bot's life %": bot,
                "sides/asset/yr": turn,
            }
        )
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    print("(sides/asset/yr: position changes a year per asset; each costs one fee)")


def part_a_4h() -> None:
    print(
        "\n=== A3. The bot's own framing on 4h bars: the last 24h move in ATR(14,4h), then the next 24h"
    )
    d = candles("240")
    rows = []
    for label, lo in (("history before the bot", None), ("bot's life (12 Oct on)", RECORD)):
        for thr in (1.0, 2.0):
            vals = []
            for a in ASSETS:
                df = d[a]
                pc = df["c"].shift(1)
                tr = pd.concat(
                    [df["h"] - df["l"], (df["h"] - pc).abs(), (df["l"] - pc).abs()], axis=1
                ).max(axis=1)
                atr = tr.rolling(14).mean()
                move = (df["c"] - df["c"].shift(6)) / atr
                fwd = df["c"].shift(-6) / df["c"] - 1
                m = move.abs() > thr
                m &= (df.index >= lo) if lo is not None else (df.index < RECORD)
                vals.append((np.sign(move) * fwd)[m].dropna())
            v = pd.concat(vals)
            rows.append(
                {
                    "period": label,
                    "24h move beyond": f"{thr:g} ATR",
                    "n (4h bars)": len(v),
                    "next 24h, in the move's direction %": v.mean() * 100,
                    "share continuing": (v > 0).mean(),
                    "t (n/6 independent)": v.mean() / v.std() * np.sqrt(len(v) / 6),
                }
            )
    print(pd.DataFrame(rows).round(3).to_string(index=False))


# ----------------------------------------------------------------------------- Part B


class Prices:
    """15-minute closes per asset; ``at`` = the last close at or before a time."""

    def __init__(self) -> None:
        df = q(
            "select symbol, open_time t, close c from candles where interval='15' and symbol = any(:s) order by open_time",
            s=SYMS,
        )
        self.t: dict[str, np.ndarray] = {}
        self.c: dict[str, np.ndarray] = {}
        for a in ASSETS:
            d = df[df.symbol == f"{a}USDT"]
            idx = pd.DatetimeIndex(pd.to_datetime(d["t"], utc=True)) + pd.Timedelta(minutes=15)
            self.t[a] = idx.as_unit("ns").asi8
            self.c[a] = d["c"].astype(float).to_numpy()

    def fwd(self, asset: str, ts: pd.Series, base: pd.Series, hours: float) -> np.ndarray:
        t, c = self.t[asset], self.c[asset]
        target = pd.DatetimeIndex(ts).as_unit("ns").asi8 + int(hours * 3600e9)
        i = np.searchsorted(t, target, side="right") - 1
        ok = (target <= t[-1]) & (i >= 0)
        out = np.full(len(target), np.nan)
        out[ok] = c[i[ok]] / base.to_numpy(float)[ok] - 1
        return out


def with_forward(df: pd.DataFrame, px: Prices) -> pd.DataFrame:
    """Add fwd_<h> columns (raw forward return of the asset) to rows with asset, ts, spot."""
    df = df.copy()
    for h in HOURS:
        df[f"fwd_{h}"] = np.nan
    for a in ASSETS:
        m = df.asset == a
        if not m.any():
            continue
        for h, hours in HOURS.items():
            df.loc[m, f"fwd_{h}"] = px.fwd(a, df.loc[m, "ts"], df.loc[m, "spot"], hours)
    return df


def part_b_calls(px: Prices) -> pd.DataFrame:
    print("\n=== B1. The calls against the drift (six assets, since the record line)")
    dec = q(
        "select d.asset, d.ts, d.spot, d.direction, d.pm1_direction, d.pm2_direction, d.pm3_direction, c.logic_version v "
        "from decisions d join cycles c on c.id = d.cycle_id where d.ts >= :since and d.asset = any(:a) and d.spot > 0",
        since=RECORD.to_pydatetime(),
        a=ASSETS,
    )
    dec["ts"] = pd.to_datetime(dec["ts"], utc=True)
    dec["spot"] = dec["spot"].astype(float)
    dec = with_forward(dec, px)
    drift = {h: dec.groupby("asset")[f"fwd_{h}"].mean() for h in HOURS}
    print("the drift itself: mean forward return of the six over every decision time, %")
    print(
        pd.DataFrame({h: (dec[f"fwd_{h}"].mean() * 100) for h in HOURS}, index=["all assets"])
        .round(2)
        .to_string()
    )
    rows = []
    for name, col in (
        ("consensus (2 of 3)", "direction"),
        ("Fable seat", "pm1_direction"),
        ("Astra seat", "pm2_direction"),
        ("Grok seat", "pm3_direction"),
    ):
        c = dec[dec[col].isin(["long", "short"])].copy()
        sign = np.where(c[col] == "long", 1.0, -1.0)
        row: dict[str, Any] = {
            "who": name,
            "calls": len(c),
            "short share": float((sign < 0).mean()),
        }
        for h in HOURS:
            got = sign * c[f"fwd_{h}"]
            exp = sign * c["asset"].map(drift[h])
            m = got.notna()
            row[f"{h} %"] = got[m].mean() * 100
            row[f"{h} drift %"] = exp[m].mean() * 100
            row[f"{h} skill %"] = (got[m] - exp[m]).mean() * 100
            row[f"{h} right"] = float((got[m] > 0).mean())
        rows.append(row)
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    print(
        "(drift: what the same mix of longs and shorts would have earned at an average moment; skill: the call minus that)"
    )
    return dec


def part_b_agents(px: Prices) -> None:
    print(
        "\n=== B2. The agents' scores against forward returns (Spearman IC), every reading and one a day"
    )
    out = q(
        "select agent, asset, computed_at ts, score from agent_outputs where valid and variant='main' "
        "and computed_at >= :since and asset = any(:a)",
        since=RECORD.to_pydatetime(),
        a=ASSETS,
    )
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    out["spot"] = np.nan
    for a in ASSETS:
        m = out.asset == a
        if m.any():
            t, c = px.t[a], px.c[a]
            i = (
                np.searchsorted(
                    t, pd.DatetimeIndex(out.loc[m, "ts"]).as_unit("ns").asi8, side="right"
                )
                - 1
            )
            out.loc[m, "spot"] = c[np.clip(i, 0, len(c) - 1)]
    out = with_forward(out, px)
    out["day"] = out["ts"].dt.floor("D")
    daily = out.sort_values("ts").groupby(["agent", "asset", "day"], as_index=False).first()
    rows = []
    for agent, g in out.groupby("agent"):
        gd = daily[daily.agent == agent]
        row: dict[str, Any] = {
            "agent": agent,
            "n": len(g),
            "n daily": len(gd),
            "score sd": g["score"].std(),
        }
        for h in HOURS:
            row[f"IC {h}"] = spearman(g["score"], g[f"fwd_{h}"])
        for h in ("1d", "3d", "7d"):
            row[f"daily IC {h}"] = spearman(gd["score"], gd[f"fwd_{h}"])
        rows.append(row)
    print(pd.DataFrame(rows).round(3).to_string(index=False))
    print(
        "(one a day: the first reading per asset per day; with six correlated assets over a month the standard error of a daily IC is about 0.1 to 0.15)"
    )


def part_b_indicators() -> None:
    print(
        "\n=== B3. The indicators score recomputed on the 4h history (same code as the agent, candle parts only)"
    )
    from app.agents.indicators import features, score_series

    d = candles("240")
    frames = []
    for a in ASSETS:
        df = d[a].rename(
            columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
        )[["open", "high", "low", "close", "volume"]]
        f = features(df, "240")
        f.attrs["interval"] = "240"
        s = score_series(f)
        x = pd.DataFrame({"asset": a, "score": s, "c": df["close"]})
        for h, bars in (("1d", 6), ("3d", 18), ("7d", 42)):
            x[f"fwd_{h}"] = x["c"].shift(-bars) / x["c"] - 1
        x["r"] = x["c"].pct_change()
        frames.append(x)
    allx = pd.concat(frames)
    rows = []
    for label, m in (
        ("before the bot", allx.index < RECORD),
        ("bot's life", allx.index >= RECORD),
    ):
        g = allx[m & allx["score"].notna()]
        row: dict[str, Any] = {"period": label, "bars": len(g)}
        for h in ("1d", "3d", "7d"):
            row[f"IC {h}"] = spearman(g["score"], g[f"fwd_{h}"])
        row["next day % when score < -0.3"] = g.loc[g.score < -0.3, "fwd_1d"].mean() * 100
        row["next day % when score > +0.3"] = g.loc[g.score > 0.3, "fwd_1d"].mean() * 100
        rows.append(row)
    print(pd.DataFrame(rows).round(3).to_string(index=False))
    rows = []
    for name, sign in (("follow the score beyond ±0.2", 1.0), ("fade the score beyond ±0.2", -1.0)):
        ports = {}
        for x in frames:
            pos = (np.sign(x["score"]).where(x["score"].abs() >= 0.2, 0.0) * sign).fillna(0.0)
            p = pos.shift(1)
            trade = (pos - pos.shift(1)).abs().shift(1).fillna(0)
            ports[x["asset"].iloc[0]] = (p * x["r"] - trade * FEE - (p < 0) * BORROW_DAY / 6).where(
                x["score"].shift(1).notna()
            )
        port = pd.DataFrame(ports).mean(axis=1).dropna()
        for label, sl in (
            ("before the bot", port[port.index < RECORD]),
            ("bot's life", port[port.index >= RECORD]),
        ):
            st = stats(sl, 6 * 365)
            rows.append(
                {
                    "rule": name,
                    "period": label,
                    "days": len(sl) / 6,
                    "ann%": st.get("ann%"),
                    "sharpe": st.get("sharpe"),
                    "maxDD%": st.get("maxDD%"),
                    "total%": st.get("total%"),
                }
            )
    print(pd.DataFrame(rows).round(2).to_string(index=False))


def part_b_trades(px: Prices) -> None:
    print("\n=== B4. The trades (primary paper book)")
    t = q(
        "select p.asset, p.direction, p.entry_price spot, p.opened_at ts, p.closed_at, p.pnl, p.fees, p.margin, p.notional, "
        "p.pnl_margin_pct m, p.pnl_price_pct pp, p.leverage lev, p.close_reason from positions p "
        "where p.mode='paper' and p.track='primary' and p.status='closed' and p.opened_at >= :since",
        since=RECORD.to_pydatetime(),
    )
    for col in ("spot", "pnl", "fees", "margin", "notional", "m", "pp", "lev"):
        t[col] = t[col].astype(float)
    t["ts"] = pd.to_datetime(t["ts"], utc=True)
    win, loss = t[t.pnl > 0], t[t.pnl <= 0]
    aw, al = win.m.mean() * 100, -loss.m.mean() * 100
    print(
        f"{len(t)} trades since the record line, {len(win)} won ({len(win) / len(t):.0%}); average win {aw:+.2f}% of margin, "
        f"average loss {-al:.2f}%; needed to break even: {al / (aw + al):.0%} winners; "
        f"result {t.pnl.sum():+.0f}, of which fees {t.fees.sum():.0f}; average leverage {t.lev.mean():.2f}x; "
        f"shorts {int((t.direction == 'short').sum())} of {len(t)}"
    )
    by = t.groupby("close_reason").agg(
        n=("pnl", "size"), pnl=("pnl", "sum"), avg_pct=("m", lambda s: s.mean() * 100)
    )
    print(by.round(1).to_string())
    six = t[t.asset.isin(ASSETS)]
    six = with_forward(six, px)
    sign = np.where(six.direction == "long", 1.0, -1.0)
    rows = [
        {
            "what": "as traded (price move the trade captured)",
            "n": len(six),
            "mean %": six.pp.mean() * 100,
            "right": float((six.pp > 0).mean()),
        }
    ]
    for h in HOURS:
        got = pd.Series(sign * six[f"fwd_{h}"].to_numpy()).dropna()
        rows.append(
            {
                "what": f"same entries, simply held {h}, no stop, no take",
                "n": len(got),
                "mean %": got.mean() * 100,
                "right": float((got > 0).mean()),
            }
        )
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    d = candles("D")
    since = RECORD.normalize()
    bh = np.mean([d[a]["c"].iloc[-1] / d[a]["c"][:since].iloc[-1] - 1 for a in ASSETS]) * 100
    print(
        f"the six assets, equal weight, bought on 12 Oct and held: {bh:+.1f}%; held short instead: {-bh:+.1f}% before borrow"
    )

    print("\n=== B5. What it costs to run")
    llm = (
        q(
            "select coalesce(sum(cost_usd),0) c from llm_calls where ts >= :since",
            since=RECORD.to_pydatetime(),
        )["c"]
        .astype(float)
        .iloc[0]
    )
    days = (pd.Timestamp.now(tz="UTC") - RECORD).days
    print(
        f"model calls since the record line: ${llm:.0f} in {days} days (${llm / days * 30:.0f} a month); "
        f"{len(t)} trades, so ${llm / len(t):.2f} of model spend per trade against an average notional of ${t.notional.mean():.0f} "
        f"({llm / len(t) / t.notional.mean() * 100:.2f}% of it; the exchange takes 0.20% a round trip)"
    )
    f = q(
        "select count(*) n, sum((direction<>'flat')::int) calls, sum((action='open')::int) approved from decisions where ts >= :since",
        since=RECORD.to_pydatetime(),
    )
    print(
        f"funnel: {int(f.n[0])} asset decisions, {int(f.calls[0])} two-of-three calls, {int(f.approved[0])} approved by the risk engine, {len(t)} trades closed"
    )


# ----------------------------------------------------------------------------- Part C


def bot_exits(d: dict[str, pd.DataFrame], lookback: int, take: float, stop: float) -> pd.Series:
    """The trend signal traded the bot's way on daily highs and lows: in with the trend at
    a close, out at +take or -stop (the stop first when a day touched both), back in at
    the same day's close while the trend still stands."""
    ports = {}
    for a in ASSETS:
        df = d[a]
        c, h, lo = df["c"].to_numpy(), df["h"].to_numpy(), df["l"].to_numpy()
        trend = np.sign(c / np.roll(c, lookback) - 1)
        trend[:lookback] = 0
        ret = np.zeros(len(c))
        side, entry = 0.0, 0.0
        for i in range(lookback + 1, len(c)):
            day = 0.0
            if side != 0:
                stop_px = entry * (1 - side * stop)
                take_px = entry * (1 + side * take)
                hit_stop = lo[i] <= stop_px if side > 0 else h[i] >= stop_px
                hit_take = h[i] >= take_px if side > 0 else lo[i] <= take_px
                if hit_stop or hit_take:
                    px = stop_px if hit_stop else take_px
                    day += side * (px / c[i - 1] - 1) - FEE
                    side = 0.0
                else:
                    day += side * (c[i] / c[i - 1] - 1) - (BORROW_DAY if side < 0 else 0.0)
            want = trend[i]
            if side != 0 and want != side:
                day -= FEE
                side = 0.0
            if side == 0 and want != 0:
                day -= FEE
                side, entry = float(want), c[i]
            ret[i] = day
        ports[a] = pd.Series(ret, index=df.index).iloc[lookback + 1 :]
    return pd.DataFrame(ports).mean(axis=1)


def bot_exits_4h(
    d: dict[str, pd.DataFrame], take: float, stop: float, cooldown: int, lookback: int = 168
) -> pd.Series:
    """The same test on 4-hour bars, where a bar rarely touches both levels, so the order
    of stop and take is no longer an assumption. ``cooldown``: bars to wait after an exit."""
    ports = {}
    for a in ASSETS:
        df = d[a]
        c, h, lo = df["c"].to_numpy(), df["h"].to_numpy(), df["l"].to_numpy()
        trend = np.sign(c / np.roll(c, lookback) - 1)
        trend[:lookback] = 0
        ret = np.zeros(len(c))
        side, entry, wait = 0.0, 0.0, 0
        for i in range(lookback + 1, len(c)):
            bar = 0.0
            if side != 0:
                stop_px = entry * (1 - side * stop)
                take_px = entry * (1 + side * take)
                hit_stop = lo[i] <= stop_px if side > 0 else h[i] >= stop_px
                hit_take = h[i] >= take_px if side > 0 else lo[i] <= take_px
                if hit_stop or hit_take:
                    px = stop_px if hit_stop else take_px
                    bar += side * (px / c[i - 1] - 1) - FEE
                    side, wait = 0.0, cooldown
                else:
                    bar += side * (c[i] / c[i - 1] - 1) - (BORROW_DAY / 6 if side < 0 else 0.0)
            want = trend[i]
            if side != 0 and want != side:
                bar -= FEE
                side = 0.0
            if side == 0 and want != 0:
                if wait > 0:
                    wait -= 1
                else:
                    bar -= FEE
                    side, entry = float(want), c[i]
            ret[i] = bar
        ports[a] = pd.Series(ret, index=df.index).iloc[lookback + 1 :]
    return pd.DataFrame(ports).mean(axis=1).dropna()


def part_c(close: pd.DataFrame, r: pd.DataFrame) -> None:
    start = "2024-06-01"  # the longest lookback is warm by then
    print(
        "\n=== C1. Is the trend rule one lucky lookback? (daily, six assets, after costs, from June 2024)"
    )
    rows = []
    for lb in (7, 14, 28, 56, 100):
        trend = np.sign(close / close.shift(lb) - 1)
        for name, pos in (("long/short", trend), ("long or cash", trend.clip(lower=0))):
            port, turn = run(pos, r)
            port = port[start:]
            st = stats(port, 365)
            first, second = stats(port[:"2025-08-31"], 365), stats(port["2025-09-01":], 365)
            rows.append(
                {
                    "lookback d": lb,
                    "book": name,
                    "cagr%": st["cagr%"],
                    "sharpe": st["sharpe"],
                    "maxDD%": st["maxDD%"],
                    "sharpe to Aug 25": first["sharpe"],
                    "sharpe since Sep 25": second["sharpe"],
                    "sides/asset/yr": turn,
                }
            )
    print(pd.DataFrame(rows).round(2).to_string(index=False))

    print(
        "\n=== C2. An ensemble of three lookbacks (14, 28, 56), then scaled to a volatility target"
    )
    ens = sum(np.sign(close / close.shift(lb) - 1) for lb in (14, 28, 56)) / 3
    vol = r.rolling(20).std() * np.sqrt(365)
    rows = []
    for name, pos in (
        ("ensemble long/short, 1x", ens),
        ("ensemble long or cash, 1x", ens.clip(lower=0)),
        (
            "ensemble long/short, 40% vol target per asset, max 1x",
            ens * (0.40 / vol).clip(upper=1.0),
        ),
        (
            "ensemble long or cash, 40% vol target per asset, max 1x",
            ens.clip(lower=0) * (0.40 / vol).clip(upper=1.0),
        ),
    ):
        port, turn = run(pos.fillna(0.0), r)
        port = port[start:]
        st = stats(port, 365)
        monthly = (1 + port).groupby([port.index.year, port.index.month]).prod() - 1
        by_year = {str(y): ((1 + g).prod() - 1) * 100 for y, g in port.groupby(port.index.year)}
        rows.append(
            {
                "book": name,
                "cagr%": st["cagr%"],
                "vol%": st["vol%"],
                "sharpe": st["sharpe"],
                "maxDD%": st["maxDD%"],
                **by_year,
                "worst month %": monthly.min() * 100,
                "months up": float((monthly > 0).mean()),
                "bot's life %": ((1 + port[RECORD.normalize() :]).prod() - 1) * 100,
                "avg |position|": float(pos[start:].abs().mean().mean()),
                "sides/asset/yr": turn,
            }
        )
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    best = run((ens * (0.40 / vol).clip(upper=1.0)).fillna(0.0), r)[0][start:]
    print(
        f"the vol-targeted long/short book, per day: mean {best.mean() * 100:+.3f}%, best day {best.max() * 100:+.1f}%, worst {best.min() * 100:+.1f}%"
    )

    print(
        "\n=== C3. The same 28-day trend signal, held while the trend holds, against the bot's exits"
    )
    d = candles("D")
    trend = np.sign(close / close.shift(28) - 1)
    rows = []
    hold = run(trend, r)[0][start:]
    for name, port in (
        ("held while the trend holds", hold),
        ("take +2.2%, stop -4%, back in at the close", bot_exits(d, 28, 0.022, 0.04)[start:]),
        ("take +2.2%, stop -6%, back in at the close", bot_exits(d, 28, 0.022, 0.06)[start:]),
        ("no take, stop -4%, back in at the close", bot_exits(d, 28, 9.0, 0.04)[start:]),
    ):
        st = stats(port, 365)
        rows.append(
            {
                "exits": name,
                "cagr%": st["cagr%"],
                "sharpe": st["sharpe"],
                "maxDD%": st["maxDD%"],
                "bot's life %": ((1 + port[RECORD.normalize() :]).prod() - 1) * 100,
            }
        )
    print(pd.DataFrame(rows).round(2).to_string(index=False))

    print(
        "\n=== C3b. The same comparison on 4-hour bars (Oct 2025 or Apr 2026 to now), 28-day trend checked every bar"
    )
    d4 = candles("240")
    rows = []
    for name, take, stop, cool in (
        ("held while the trend holds (stop and take out of reach)", 9.0, 9.0, 0),
        ("take +2.2%, stop -4%, back in at once", 0.022, 0.04, 0),
        ("take +2.2%, stop -4%, back in after a day", 0.022, 0.04, 6),
        ("take +2.2%, stop -6%, back in after a day", 0.022, 0.06, 6),
        ("take +6%, stop -4%, back in after a day", 0.06, 0.04, 6),
        ("no take, stop -4%, back in after a day", 9.0, 0.04, 6),
    ):
        port = bot_exits_4h(d4, take, stop, cool)
        st = stats(port, 6 * 365)
        life = port[port.index >= RECORD]
        rows.append(
            {
                "exits": name,
                "days": len(port) / 6,
                "cagr%": st["cagr%"],
                "sharpe": st["sharpe"],
                "maxDD%": st["maxDD%"],
                "total%": st["total%"],
                "bot's life %": ((1 + life).prod() - 1) * 100,
            }
        )
    print(pd.DataFrame(rows).round(2).to_string(index=False))

    print("\n=== C4. How much of the month's fall did the book actually hold short?")
    e = q(
        "select sum(notional * extract(epoch from (coalesce(closed_at, now()) - opened_at))) / extract(epoch from (now() - :since)) avg_notional, "
        "sum(case when direction='short' then 1 else -1 end * notional * extract(epoch from (coalesce(closed_at, now()) - opened_at))) / extract(epoch from (now() - :since)) net_short "
        "from positions where mode='paper' and track='primary' and opened_at >= :since",
        since=RECORD.to_pydatetime(),
    )
    print(
        f"average open notional {float(e.avg_notional[0]):,.0f} on a 10,000 book, net short {float(e.net_short[0]):,.0f}"
    )
    c = q(
        "select task, round(sum(cost_usd)::numeric,2) usd, count(*) n from llm_calls where ts >= now() - interval '30 days' group by 1 order by 2 desc limit 8"
    )
    print("model spend by task, last 30 days:")
    print(c.to_string(index=False))


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "acb"
    if "a" in which:
        close, r = daily_frames()
        print(
            f"daily history: {close.index[0].date()} to {close.index[-1].date()}, {len(close)} days"
        )
        part_a_structure(close, r)
        part_a_strategies(close, r)
        part_a_4h()
    if "c" in which:
        close, r = daily_frames()
        part_c(close, r)
    if "b" in which:
        px = Prices()
        part_b_calls(px)
        part_b_agents(px)
        part_b_indicators()
        part_b_trades(px)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
