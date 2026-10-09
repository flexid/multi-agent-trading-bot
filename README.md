# multi-agent-trading-bot

An autonomous crypto trader on Bybit EU. Five analysis agents score BTC, ETH, SOL, BNB and SPX6900 every 4 hours; Claude and GPT independently turn those scores into trade proposals; a deterministic risk engine decides, and a separate executor places and manages the orders. Every trade is posted to X.

> **Status:** M0 (connectivity) complete, including the live order self-test; see [`docs/M0_REPORT.md`](docs/M0_REPORT.md). M1 (data layer) complete. No trading logic yet.

## How it works

```
data layer (15 min) → 5 agents → PM 1 + PM 2 → leverage agent → risk engine → executor
                                                                            → X poster
                                                                            → dashboard
```

| Layer | What it does |
| --- | --- |
| Data | Bybit candles and order books, Polymarket odds, X posts, macro data (rates, dollar, VIX, equities, gold) |
| Agents | Macro (with crypto–tradfi coupling), chart patterns, indicators, Polymarket, X sentiment. Each outputs a score per asset |
| Portfolio managers | Claude Fable 5.1 and GPT-5.6 Sol, same input, no sight of each other. A trade needs both to agree |
| Leverage agent | 1x–10x from stop distance, volatility, events and liquidity; can only be lowered by an LLM, never raised |
| Risk engine | Consensus, exposure and liquidity caps, daily loss stop, drawdown pause, emergency brake |
| Executor | Separate process; limit orders, stops and time-stops in code on the WebSocket; paper simulator for shadow mode |
| X poster | Casual post per open and close on @decentradork, cashtag always, amounts never |

## Safety principles

- No LLM ever touches an order.
- Every model output is schema-validated; anything invalid means no new trade.
- Live trading only after at least 6 weeks of shadow mode that meets the criteria in [SPEC §10](docs/SPEC.md#10-shadow-mode-and-automatic-go-live), and only when `live_allowed = true`.
- Secrets stay in `.env`, never in git, logs or prompts.

## Repository

| Path | Contents |
| --- | --- |
| [`CLAUDE.md`](CLAUDE.md) | Rules and conventions for Claude Code |
| [`docs/SPEC.md`](docs/SPEC.md) | Full build spec and milestones |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Decision log |
| [`docs/M0_REPORT.md`](docs/M0_REPORT.md), [`docs/M1_REPORT.md`](docs/M1_REPORT.md) | Milestone reports: what works, what doesn't, what the owner must provide |
| [`config.toml`](config.toml) | Bot parameters (the only thing the owner tunes) |
| [`.env.example`](.env.example) | Required secrets, copy to `.env` |

Code (`app/`, `api/`, `web/`, `tests/`) arrives milestone by milestone.

## Getting started

Prerequisites: Python 3.12 with [uv](https://docs.astral.sh/uv/), Docker with Compose, Node.js for the dashboard, a server outside the US (Bybit refuses US IPs).

```bash
cp .env.example .env    # fill in the keys
uv sync
make test lint
make selftest           # read-only checks + paper round-trip
```

## M1: data layer

Postgres 16 (`make up`), schema via Alembic (`make migrate`). `make fetch-once` runs every fetch job one time; `make scheduler` runs them every 15 minutes (macro hourly).

| Source | Stored | Table |
| --- | --- | --- |
| Bybit candles | 15m / 1h / 4h / 1D, closed bars, 200 per fetch, upserted | `candles` |
| Bybit order books | top 25 levels, mid, spread, ±2% depth | `orderbook_snapshots` |
| Bybit account | balances, borrows, open orders | `account_snapshots` |
| Polymarket | top 100 markets by 24h volume, prices appended | `polymarket_markets`, `polymarket_prices` |
| FRED | 2y/10y yields, dollar index, VIX, S&P 500, Nasdaq | `macro_observations` |

Every job writes a `fetch_runs` row and updates `data_sources` (last success, last error); `app.data.fetch.is_fresh()` applies the 30-minute limit the agents use. Code: [`app/db/models.py`](app/db/models.py), [`app/data/fetch.py`](app/data/fetch.py), [`app/scheduler.py`](app/scheduler.py).

## M0: connectivity self-test

`make selftest` checks the exchange clock, all five pairs (listing, tick and lot size, liquidity, margin and borrow terms), the API key, account mode, balances, fee rates, a paper round-trip, Polymarket and X. It never sends an order: the Bybit client refuses order calls unless it was built for them.

| Command | What it does |
| --- | --- |
| `make selftest` | All read-only checks. The X read costs about $0.06; skip it with `ARGS=--no-x` |
| `make selftest ARGS="--json logs/selftest.json"` | Same, and saves the results |
| `make selftest-live CONFIRM=yes` | Also places one minimal post-only order 10% below the bid and cancels it |
| `make bybit-authorize` | Connects the bot to a Bybit AI Subaccount via OAuth and writes its key into `.env` (`ARGS=--list`, `--use <id>`, `--create`) |
| `make up` / `make down` | Postgres 16 (used from M1) |

Code: [`app/execution/bybit_client.py`](app/execution/bybit_client.py), [`app/data/polymarket.py`](app/data/polymarket.py), [`app/data/x.py`](app/data/x.py), [`app/selftest.py`](app/selftest.py).

## Milestones

| # | Milestone | Status |
| --- | --- | --- |
| M0 | Connectivity: Bybit, Polymarket, X self-tests | done |
| M1 | Data layer and database | done |
| M2 | Indicators agent and backtest harness | – |
| M3 | Macro, chart, Polymarket and X agents | – |
| M4 | Decision layer: two PMs, consensus | – |
| M5 | Leverage agent and risk engine | – |
| M6 | Executor, paper simulator — shadow mode starts | – |
| M7 | X poster | – |
| M8 | Dashboard | – |
| M9 | Automatic go-live checker, leverage ramp, weight tuning | – |

Details per milestone: [SPEC §14](docs/SPEC.md#14-milestones).
