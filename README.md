# multi-agent-trading-bot

An autonomous crypto trader on Bybit EU. Five analysis agents score BTC, ETH, SOL, BNB and SPX6900 every 4 hours; Claude and GPT independently turn those scores into trade proposals; a deterministic risk engine decides, and a separate executor places and manages the orders. Every trade is posted to X.

> **Status:** M0 (connectivity) complete, including the live order self-test; see [`docs/M0_REPORT.md`](docs/M0_REPORT.md). M6 complete: shadow mode is running on the server (paper trades on two tracks). Live trading waits for the go-live checker (M9).

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
| [`docs/M0_REPORT.md`](docs/M0_REPORT.md), [`docs/M1_REPORT.md`](docs/M1_REPORT.md), [`docs/M2_REPORT.md`](docs/M2_REPORT.md), [`docs/M3_REPORT.md`](docs/M3_REPORT.md), [`docs/M4_REPORT.md`](docs/M4_REPORT.md), [`docs/M5_REPORT.md`](docs/M5_REPORT.md), [`docs/M6_REPORT.md`](docs/M6_REPORT.md), [`docs/M7_REPORT.md`](docs/M7_REPORT.md) | Milestone reports: what works, what doesn't, what the owner must provide |
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

## Owner's terminal report

`dorkbot` (optionally `dorkbot 72` for a 72-hour window) prints every trade on the primary paper track in the window, day and total P&L, and today's LLM and X cost, over SSH from the server. Add to `~/.zshrc`:

```zsh
dorkbot() { ssh root@164.90.211.109 "cd /opt/dorkbot && docker compose --profile bot run --rm -T scheduler python -m app.report ${1:-24}"; }
```

## M6: executor and shadow mode

The executor ([`app/execution/executor.py`](app/execution/executor.py)) is its own process: it opens paper positions for sized decisions through an order gateway ([`gateway.py`](app/execution/gateway.py)) and enforces stops, targets, trailing stops, time-stops and liquidation in code ([`simulator.py`](app/execution/simulator.py)). Two shadow tracks (live rules vs `leverage_max`), kill switch (`make kill`), heartbeats, equity snapshots. `make deploy` runs everything on the droplet with Docker Compose. Details and the seven failure-mode tests in [`docs/M6_REPORT.md`](docs/M6_REPORT.md), [`docs/M7_REPORT.md`](docs/M7_REPORT.md).

## M5: leverage agent and risk engine

Pure functions in [`app/risk/leverage.py`](app/risk/leverage.py) (base formula and the caps table of SPEC §8, LLM may only lower) and [`app/risk/engine.py`](app/risk/engine.py) (account limits, per-trade gates, cost rule, depth and exposure caps, liquidation buffer). [`app/risk/apply.py`](app/risk/apply.py) runs them at the end of each cycle and writes rule hits and the action to `decisions`. Details in [`docs/M5_REPORT.md`](docs/M5_REPORT.md), [`docs/M6_REPORT.md`](docs/M6_REPORT.md), [`docs/M7_REPORT.md`](docs/M7_REPORT.md).

## M4: decision layer

`python -m app.decision.cycle` runs one cycle: the five agents in parallel, the evidence pack ([`app/decision/evidence.py`](app/decision/evidence.py)), both PMs on the same pack ([`pm.py`](app/decision/pm.py), prompt [`app/prompts/pm.md`](app/prompts/pm.md)), formula anchor and consensus ([`consensus.py`](app/decision/consensus.py)), all logged to `cycles`, `agent_outputs`, `pm_proposals` and `decisions`. Rules: fewer than 3 valid agents or any PM failure → no trade; opposite directions → no trade; one PM flat → trade marked partial (1x, no borrowing in M5); consensus clamped to formula ± 0.4. Details in [`docs/M4_REPORT.md`](docs/M4_REPORT.md), [`docs/M5_REPORT.md`](docs/M5_REPORT.md), [`docs/M6_REPORT.md`](docs/M6_REPORT.md), [`docs/M7_REPORT.md`](docs/M7_REPORT.md).

## M3: LLM layer and agents

Every model call goes through [`app/llm/`](app/llm/): model from `config.toml`, provider by prefix, pydantic-validated output, versioned prompts in [`app/prompts/`](app/prompts/), one retry then fail closed, and a row in `llm_calls` with tokens and cost. Agents: macro ([`app/agents/macro.py`](app/agents/macro.py)), chart patterns ([`chart_patterns.py`](app/agents/chart_patterns.py)), Polymarket ([`polymarket.py`](app/agents/polymarket.py) + daily [`polymarket_map.py`](app/agents/polymarket_map.py)), X sentiment ([`x_sentiment.py`](app/agents/x_sentiment.py)). Runners: `python -m app.agents.run_<name>`. Details and live costs in [`docs/M3_REPORT.md`](docs/M3_REPORT.md), [`docs/M4_REPORT.md`](docs/M4_REPORT.md), [`docs/M5_REPORT.md`](docs/M5_REPORT.md), [`docs/M6_REPORT.md`](docs/M6_REPORT.md), [`docs/M7_REPORT.md`](docs/M7_REPORT.md).

## M2: indicators agent and backtest

Deterministic scores from fixed rules on closed candles ([`app/agents/indicators.py`](app/agents/indicators.py)): EMA 20/50/200 stack scaled by ADX, MACD histogram over ATR, RSI, OBV, perp funding and open-interest change. Output follows the agent contract in [`app/agents/schema.py`](app/agents/schema.py) (score, confidence, horizon, evidence, risk flags, data age; stale after 30 minutes).

| Command | What it does |
| --- | --- |
| `make indicators` | Scores all five assets from the stored 4h candles and perp metrics |
| `make backtest ARGS="--interval 240 --days 180"` | IC, hit rate and top-minus-bottom-quintile spread of the score against 4h, 1d and 3d forward returns |

Only code-based agents are backtested (SPEC §10). Current result: no measurable edge over the last 180 days; see [`docs/M2_REPORT.md`](docs/M2_REPORT.md), [`docs/M3_REPORT.md`](docs/M3_REPORT.md), [`docs/M4_REPORT.md`](docs/M4_REPORT.md), [`docs/M5_REPORT.md`](docs/M5_REPORT.md), [`docs/M6_REPORT.md`](docs/M6_REPORT.md), [`docs/M7_REPORT.md`](docs/M7_REPORT.md).

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
| M2 | Indicators agent and backtest harness | done |
| M3 | LLM layer; macro, chart, Polymarket and X agents | done |
| M4 | Decision layer: two PMs, consensus | done |
| M5 | Leverage agent and risk engine | done |
| M6 | Executor, paper simulator — shadow mode starts | done; shadow running since 2026-10-09 |
| M7 | X poster | built, dry-run |
| M8 | Dashboard | – |
| M9 | Automatic go-live checker, leverage ramp, weight tuning | – |

Details per milestone: [SPEC §14](docs/SPEC.md#14-milestones).
