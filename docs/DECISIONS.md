# Decision log

Newest first. One entry per decision: what was decided, why, and what it rules out.

## 2026-10-09 · M1 data layer: sources and storage

- **Macro from FRED only for now:** DGS2, DGS10, DTWEXBGS (broad dollar), VIXCLS, SP500, NASDAQCOM, hourly, 60-day window upserted. Gold is no longer on FRED; gold, the economic calendar, spot ETF flows and stablecoin supply are chosen in M3 with the macro agent that consumes them.
- **Polymarket:** top 100 markets by 24-hour volume every 15 minutes (Gamma caps a page at 100), metadata upserted, prices appended. The market `question` is stored because the dashboard and the daily asset-mapping prompt need it; it is untrusted text and never reaches the PMs.
- **Candles:** 15m, 1h, 4h, 1D, 200 bars per fetch, closed bars only, upserted on (symbol, interval, open_time). Order books: top 25 levels plus ±2% depth per snapshot, appended.
- **Freshness:** `data_sources.last_success_at` per source; `is_fresh()` with the 30-minute limit from SPEC §6. A failed fetch keeps the last success and increments `consecutive_errors`.
- **Sync SQLAlchemy with psycopg 3**, APScheduler 3 on asyncio. Volume is tiny; async DB adds nothing.
- **X reads are not scheduled in M1.** They are billed per post and belong to the X agent (M3), which decides what to read within the daily budget.

## 2026-10-09 · No Telegram (owner)

Telegram is dropped completely: no alerts, no daily report, no kill-switch command, no `app/notify/`. The dashboard is the only place the bot reports, and it carries the kill switch and the emergency-brake notice.
Rules out: push notifications. An emergency brake is only seen when the owner opens the dashboard.

## 2026-10-09 · Quote coin is USDT (owner)

All five assets trade against USDT: `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `BNBUSDT`, `SPXUSDT`. Supersedes the USDC entry below. `alt_quote = "USDC"` remains for comparison in the self-test only.
Why: on Bybit global only the USDT pairs of BNB and SPX6900 have margin and usable liquidity. Cost: taker fee 0.10% instead of 0.05%.

## 2026-10-09 · M0: self-test results go to a JSON file

`make selftest` prints its results and can write them to `logs/selftest.json`. Postgres logging starts with the schema in M1.

## 2026-10-09 · Quote coin stays USDC until the owner chooses

`[exchange] quote = "USDC"` as in the spec. The self-test also measures the USDT pairs (`alt_quote`), because on Bybit global `BNBUSDC` and `SPXUSDC` have no margin and almost no liquidity while the USDT pairs have both. See `docs/M0_REPORT.md`.
Why not switched: the quote coin decides which stablecoin the owner funds. Rules out: nothing yet; the bot trades only `quote`.

## 2026-10-09 · `make selftest-live` is exempt from `live_allowed`

The live self-test is gated by `--live --confirm yes` only. It sends one post-only limit buy at the minimum size, 10% below the bid, and cancels it.
Why: SPEC §10 makes this self-test a precondition for going live, so it cannot depend on `live_allowed`. Rules out: any other order path outside the executor. A `BybitClient` refuses order calls unless built with `allow_orders=True`.

## 2026-10-09 · Own thin Bybit client instead of `pybit`

`app/execution/bybit_client.py`: async httpx, HMAC signing per the V5 docs, pydantic models with `Decimal`, category fixed to `spot`.
Why: `pybit` is synchronous, returns untyped dicts and floats, and exposes every product. Rules out: derivatives calls by construction.

## 2026-10-09 · Host is `api.bybit.com` (owner)

The API key was created on Bybit global; `api.bybit.eu` rejects it. The owner switched `BYBIT_BASE_URL` to `https://api.bybit.com` during M0. This supersedes "Bybit EU" in the entry below and in SPEC §3 as far as the host goes.
The product scope does not change: spot and spot margin only, no perpetuals, even though the global platform offers them.

## 2026-10-08 · LLMs advise, code decides

Taken before the build. Five analysis agents and two LLM portfolio managers (Claude Fable 5.1 and GPT-5.6 Sol) only produce scored, schema-validated proposals. A deterministic risk engine decides, and a separate executor is the only component that touches orders.
Why: model output can be wrong, late or manipulated through untrusted input (X posts). Rules out: any tool access for models to the exchange.

## 2026-10-08 · Bybit EU, spot and spot margin only

The owner is an EEA resident, so the account sits with Bybit EU (MiCA licence): spot plus spot margin up to 10x, cross margin only. No perpetuals, no TradFi products. Shorts only through borrowing on spot margin, where a pair allows it.
Why: regulatory scope of Bybit EU. Rules out: perps, funding-rate strategies, index products.

## 2026-10-08 · SPX means SPX6900

The fifth asset is the SPX6900 memecoin (`SPXUSDC`), not the S&P 500. Its listing on Bybit EU is verified in M0; if it is missing, the bot trades the other four and keeps SPX6900 on the dashboard only. Hard leverage cap 3x because of thin books and high volatility.

## 2026-10-08 · Fully autonomous, forward-tested

No human review or approval. The owner sets parameters once and flips `live_allowed` once; going live happens automatically after at least 6 weeks of shadow mode that meets SPEC §10. The single human touchpoint is the emergency brake.
Why: owner requirement. Backtests with LLMs in the loop suffer look-ahead, so the forward test is the real validation.

## 2026-10-08 · Trades posted to X as @decentradork

Every fill is posted in casual, human language: cashtag, entry, leverage, stop, target on open; exit, % result and holding time on close. Never amounts. Claude writes, GPT audits, code whitelists the numbers.
