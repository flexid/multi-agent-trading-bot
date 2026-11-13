"""Out-of-sample check for the November 2026 review: the rules declared in
scripts/review_evidence.py (parts A and C), unchanged, on daily candles from 2018, which
neither the bot nor the review had looked at. Public Binance spot candles, no key.

uv run python -m scripts.review_history [cache_dir]
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ASSETS = ["BTC", "ETH", "XRP", "DOGE", "SOL", "ENA"]
FEE = 0.001
BORROW_DAY = 0.04 / 365
URL = "https://api.binance.com/api/v3/klines?symbol={s}USDT&interval=1d&startTime={t}&limit=1000"
START_MS = 1502928000000  # 2017-08-17, the first BTCUSDT candle
pd.set_option("display.width", 220)


def fetch(asset: str, cache: Path) -> pd.DataFrame:
    path = cache / f"{asset}.json"
    if path.exists():
        rows = json.loads(path.read_text())
    else:
        rows, t = [], START_MS
        while True:
            with urllib.request.urlopen(URL.format(s=asset, t=t), timeout=30) as resp:
                page = json.loads(resp.read())
            if not page:
                break
            rows += page
            t = page[-1][0] + 86_400_000
            if len(page) < 1000:
                break
            time.sleep(0.3)
        path.write_text(json.dumps(rows))
    df = pd.DataFrame([r[:6] for r in rows], columns=["t", "o", "h", "l", "c", "v"]).astype(float)
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    return df.set_index("t").iloc[:-1]  # the running candle off


def stats(ret: pd.Series) -> dict[str, float]:
    ret = ret.dropna()
    if len(ret) < 30:
        return {"cagr%": np.nan, "sharpe": np.nan, "maxDD%": np.nan}
    eq = (1 + ret).cumprod()
    vol = ret.std() * np.sqrt(365)
    return {
        "cagr%": (eq.iloc[-1] ** (365 / len(ret)) - 1) * 100,
        "sharpe": ret.mean() * 365 / vol if vol > 0 else np.nan,
        "maxDD%": (eq / eq.cummax() - 1).min() * 100,
    }


def run(pos: pd.DataFrame, r: pd.DataFrame) -> pd.Series:
    p = pos.shift(1)
    trade = (pos - pos.shift(1)).abs().shift(1)
    net = p * r - trade.fillna(0) * FEE - (p < 0) * BORROW_DAY
    return net.where(r.notna()).mean(axis=1)


def take_stop(d: dict[str, pd.DataFrame], lookback: int, take: float, stop: float) -> pd.Series:
    """The 28-day trend traded with a take and a stop on daily highs and lows, back in at
    the close (the stop first when a day touched both)."""
    ports = {}
    for a, df in d.items():
        c, h, lo = df["c"].to_numpy(), df["h"].to_numpy(), df["l"].to_numpy()
        trend = np.sign(c / np.roll(c, lookback) - 1)
        trend[:lookback] = 0
        ret = np.zeros(len(c))
        side, entry = 0.0, 0.0
        for i in range(lookback + 1, len(c)):
            day = 0.0
            if side != 0:
                stop_px, take_px = entry * (1 - side * stop), entry * (1 + side * take)
                hit_stop = lo[i] <= stop_px if side > 0 else h[i] >= stop_px
                hit_take = h[i] >= take_px if side > 0 else lo[i] <= take_px
                if hit_stop or hit_take:
                    day += side * ((stop_px if hit_stop else take_px) / c[i - 1] - 1) - FEE
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


def whole_book(
    d: dict[str, pd.DataFrame],
    ens: pd.DataFrame,
    weight: pd.DataFrame,
    side: int,
    stop: float,
) -> tuple[pd.Series, float]:
    """The book as it is built (app/strategy/trend.py, app/risk/rulebook.py): one position
    per asset, opened whole at a daily close when the majority of the three votes is on
    ``side``, sized by the volatility weight of that day, closed whole when the majority
    turns. A disaster stop ``stop`` from the entry fires on the day's low or high; after
    it the asset sits out until the signal has turned off and on again. Returns the
    portfolio's daily return (equal weight over what was listed) and orders a year."""
    ports = {}
    orders = 0
    for a, df in d.items():
        c, h, lo = df["c"].to_numpy(), df["h"].to_numpy(), df["l"].to_numpy()
        e = ens[a].reindex(df.index).to_numpy() * side
        ww = weight[a].reindex(df.index).to_numpy()
        ret = np.zeros(len(c))
        pos, entry, blocked = 0.0, 0.0, False
        for i in range(57, len(c)):
            day = 0.0
            if pos != 0:
                px = entry * (1 - side * stop)
                hit = lo[i] <= px if side > 0 else h[i] >= px
                if hit:
                    day += pos * side * (px / c[i - 1] - 1) - pos * FEE
                    pos, blocked = 0.0, True
                    orders += 1
                else:
                    day += pos * side * (c[i] / c[i - 1] - 1) - (
                        pos * BORROW_DAY if side < 0 else 0
                    )
            on = bool(np.isfinite(e[i]) and e[i] > 0)
            if not on:
                blocked = False
                if pos != 0:
                    day -= pos * FEE
                    pos = 0.0
                    orders += 1
            elif pos == 0 and not blocked and np.isfinite(ww[i]) and ww[i] > 0:
                pos, entry = float(ww[i]), c[i]
                day -= pos * FEE
                orders += 1
            ret[i] = day
        s = pd.Series(ret, index=df.index).iloc[57:]
        ports[a] = s.where(df["c"].pct_change().reindex(s.index).notna())
    port = pd.DataFrame(ports).mean(axis=1)["2018-01-01":]
    return port, orders / (len(port) / 365)


def built(d: dict[str, pd.DataFrame], close: pd.DataFrame, r: pd.DataFrame) -> None:
    print(
        "\n=== the books as built: whole positions on 2 of 3 votes, 25% volatility target, disaster stop 15%"
    )
    vol = r.rolling(20).std() * np.sqrt(365)
    ens = sum(np.sign(close / close.shift(lb) - 1) for lb in (14, 28, 56)) / 3
    weight = (0.25 / vol).clip(upper=1.0)
    rows = []
    for name, side in (("trend book: long or cash", 1), ("short book: short or cash", -1)):
        port, orders = whole_book(d, ens, weight, side, 0.15)
        st, recent = stats(port), stats(port["2022-01-01":])
        yearly = (1 + port).groupby(port.index.year).prod() - 1
        monthly = (1 + port).groupby([port.index.year, port.index.month]).prod() - 1
        rows.append(
            {
                "book": name,
                "sharpe all": st["sharpe"],
                "sharpe since 2022": recent["sharpe"],
                "cagr% all": st["cagr%"],
                "cagr% since 2022": recent["cagr%"],
                "maxDD%": st["maxDD%"],
                "worst year %": yearly.min() * 100,
                "worst month %": monthly.min() * 100,
                "since 12 Oct 2026 %": ((1 + port["2026-10-12":]).prod() - 1) * 100,
                "orders/yr": orders,
            }
        )
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    port, _ = whole_book(d, ens, weight, 1, 0.15)
    yearly = (1 + port).groupby(port.index.year).prod() - 1
    print(
        "trend book, year by year, %: "
        + ", ".join(f"{y} {v * 100:+.0f}" for y, v in yearly.items())
    )


def main() -> int:
    cache = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/dorkbot-history")
    cache.mkdir(parents=True, exist_ok=True)
    d = {a: fetch(a, cache) for a in ASSETS}
    for a, df in d.items():
        print(f"{a}: {df.index[0].date()} to {df.index[-1].date()}, {len(df)} days")
    close = pd.DataFrame({a: d[a]["c"] for a in ASSETS})
    r = close.pct_change()
    sigma = r.rolling(20).std().shift(1)
    z = r / sigma
    vol = r.rolling(20).std() * np.sqrt(365)
    ens = sum(np.sign(close / close.shift(lb) - 1) for lb in (14, 28, 56)) / 3
    trend28 = np.sign(close / close.shift(28) - 1)
    books = {
        "hold (long everything listed)": run(close.notna().astype(float), r),
        "trend 28d, long/short": run(trend28, r),
        "ensemble 14/28/56, long/short, 1x": run(ens, r),
        "ensemble, long or cash, 1x": run(ens.clip(lower=0), r),
        "ensemble, long/short, 40% vol target": run(
            (ens * (0.40 / vol).clip(upper=1.0)).fillna(0.0), r
        ),
        "ensemble, long or cash, 40% vol target": run(
            (ens.clip(lower=0) * (0.40 / vol).clip(upper=1.0)).fillna(0.0), r
        ),
        "ensemble, short or cash, 40% vol target": run(
            (ens.clip(upper=0) * (0.40 / vol).clip(upper=1.0)).fillna(0.0), r
        ),
        "long/short vol target, BTC ETH XRP only (the 2018 top three)": run(
            (ens * (0.40 / vol).clip(upper=1.0)).fillna(0.0)[["BTC", "ETH", "XRP"]],
            r[["BTC", "ETH", "XRP"]],
        ),
        "long/short vol target, BTC and ETH only": run(
            (ens * (0.40 / vol).clip(upper=1.0)).fillna(0.0)[["BTC", "ETH"]], r[["BTC", "ETH"]]
        ),
        "chase a >1σ day, 1 day": run(np.sign(r).where(z.abs() > 1, 0.0), r),
        "fade a >1σ day, 1 day": run(-np.sign(r).where(z.abs() > 1, 0.0), r),
        "trend 28d with take +2.2%, stop -4%": take_stop(d, 28, 0.022, 0.04),
        "trend 28d with no take, stop -4%": take_stop(d, 28, 9.0, 0.04),
    }
    eras = {
        "2018-19": ("2018-01-01", "2019-12-31"),
        "2020-21": ("2020-01-01", "2021-12-31"),
        "2022-23": ("2022-01-01", "2023-12-31"),
        "2024-26 (seen)": ("2024-01-01", "2030-01-01"),
        "2018-23 (unseen)": ("2018-01-01", "2023-12-31"),
        "all": ("2018-01-01", "2030-01-01"),
    }
    for metric in ("sharpe", "cagr%", "maxDD%"):
        rows = []
        for name, port in books.items():
            rows.append(
                {"book": name, **{e: stats(port[a:b])[metric] for e, (a, b) in eras.items()}}
            )
        print(f"\n=== {metric}, after 0.1% a side and borrow, equal weight over what was listed")
        print(pd.DataFrame(rows).round(2).to_string(index=False))

    main_book = books["ensemble, long/short, 40% vol target"]["2018-01-01":]
    unseen = main_book[:"2023-12-31"]
    yearly = (1 + main_book).groupby(main_book.index.year).prod() - 1
    print("\n=== the vol-targeted long/short ensemble, year by year, %")
    print((yearly * 100).round(1).to_string())
    for label, s in (("2018-23, unseen", unseen), ("2018 to now", main_book)):
        s = s.dropna()
        t = s.mean() / s.std() * np.sqrt(len(s))
        monthly = (1 + s).groupby([s.index.year, s.index.month]).prod() - 1
        print(
            f"{label}: mean per day {s.mean() * 100:+.3f}%, t-stat {t:.2f} on {len(s)} days, "
            f"months up {float((monthly > 0).mean()):.0%}, worst month {monthly.min() * 100:.1f}%, best {monthly.max() * 100:.1f}%"
        )
    built(d, close, r)
    print("\n=== does a big day continue? next day in the move's direction, %, by era (pooled)")
    cont = (np.sign(r) * r.shift(-1)).where(z.abs() > 1)
    rows = []
    for e, (a, b) in eras.items():
        v = cont[a:b].stack().dropna()
        rows.append(
            {
                "era": e,
                "n": len(v),
                "mean %": v.mean() * 100,
                "t (n/3 independent)": v.mean() / v.std() * np.sqrt(len(v) / 3),
            }
        )
    print(pd.DataFrame(rows).round(3).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
