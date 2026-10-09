# M1 report: data layer and database

Commit `ca3bfca`, 2026-10-09. Lint, mypy and 28 tests pass; two tests run against the local Postgres and skip when it is down.

## What works

| Piece | State |
| --- | --- |
| Postgres 16 in Docker (`make up`), schema with Alembic (`make migrate`) | Running locally, migration `652b91d87761` applied |
| Bybit candles: 15m / 1h / 4h / 1D for all five USDT pairs, closed bars only, upserted | First pass stored 3,980 bars; 199 per symbol and interval (1D back to 2026-03-24) |
| Bybit order books: top 25 levels, mid, spread, ±2% depth | One snapshot per pair per fetch |
| Bybit account: balances, borrows, open orders | One snapshot per fetch; reconciliation input for M6 |
| Polymarket: top 100 markets by 24h volume, metadata upserted, prices appended | 100 markets per fetch |
| FRED: 2y and 10y yields, broad dollar index, VIX, S&P 500, Nasdaq | 251 observations, 60-day window, hourly refresh |
| Freshness: `fetch_runs` per job, `data_sources` per source, `is_fresh()` with the 30-minute limit | Every source fresh after the first pass |
| Scheduler: `make scheduler` (every 15 min at :20 s, macro hourly), `make fetch-once` | Verified with one full pass |

## What doesn't work yet, or is deferred

| Item | Finding |
| --- | --- |
| Gold, economic calendar, ETF flows, stablecoin supply | Not fetched. FRED dropped its gold series. Sources are picked in M3 with the macro agent that consumes them. |
| X reads | Not scheduled on purpose: billed per post, so the X agent (M3) decides what to read within the 600/day budget. |
| Perp funding and open interest | Added at the start of M2 (`perp_metrics`, `bybit_global.perps` job). |
| Polymarket page size | Gamma returns at most 100 markets per page; enough for the five assets, pagination can come later. |
| FRED key in logs | httpx logged request URLs at INFO level on the first run, and FRED carries the key in the URL. The httpx logger is now set to WARNING in the scheduler. Rotating the key is optional. |
| The scheduler is not running continuously | It runs when started by hand. It becomes a service with Docker Compose when the server exists (M6). |

## Decisions

See `docs/DECISIONS.md`, entry "M1 data layer: sources and storage".

## Needed from the owner

Nothing for M1 or M2. Still open from M0:

1. Curated X account list (50–100 handles) or permission to reuse the retweet-mirror list; needed by M3.
2. Subaccount permissions on `AIsub592625865`: Request Transfer In/Out off, cap limit.
3. Delete the old `decentradork` master-account key.
4. Server IP, when the server exists; the current key expires 2027-01-07 and the self-test warns 30 days ahead.
