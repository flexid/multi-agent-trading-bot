# Decision log

Newest first. One entry per decision: what was decided, why, and what it rules out.

## 2026-10-09 · Crypto-native macro inputs: Fear & Greed weighted 0.25, dominance 0 (owner briefing)

Owner briefing: add Fear & Greed (alternative.me, daily since 2018) and BTC dominance (CoinGecko `/global` every 15 min, plus ETHBTC/SOLBTC candles as a backtestable proxy) to the macro agent as crypto-native inputs; split the macro output into a tradfi part × coupling and a native part not scaled by coupling; log both; backtest before weighting; show both on the public agents page.
Backtest (`python -m app.backtest_native`, daily bars, ~998 per asset, 2024-01 to 2026-10):
- Fear & Greed (contrarian at extremes >80 / <20): IC within ±0.02 at 1 and 3 days; hit rate on the extreme readings 59% BTC, 61% SPX6900, 50–55% elsewhere at 3 days; quintile spreads mixed (BTC −0.36%, SPX6900 +1.32%). A weak caution signal. Weight 0.25 of the native part, plus the "fear & greed extreme" risk flag for the leverage agent.
- Dominance proxy (ETHBTC 7-day change, inverted; rising dominance = +BTC, −alts, ×1.5 SPX6900): IC within ±0.04 and quintile spreads zero to negative on every asset (SOL −0.76%, SPX6900 −0.77% at 3 days). Weight 0 (`[agents.macro] dominance_weight`), logged every cycle in `agent_outputs.components`. Re-run the backtest when 60 days of real CoinGecko dominance exist.
Rules out: the dominance tilt moving a score before it earns it.

## 2026-10-09 · M4: one PM flat is "partial", not "no trade"

SPEC §7 says opposite directions mean no trade and §8 caps "PMs disagree" at 1x without borrowing. Long-versus-flat is treated as that disagreement: the directional PM's proposal proceeds with conviction capped below 0.5, so the risk engine applies the 1x rule. Long-versus-short stays a hard no. Consensus geometry (entry zone, stop, target, hold time) is the conviction-weighted mean of the two proposals when they agree, with conviction equal to the lower of the two.
Rules out: trading on a single PM at leverage.

## 2026-10-09 · M4: alternate-provider PMs run every cycle in shadow mode

Both PMs also run on the swapped providers (`[models.shadow_alt]`) and their proposals are stored with `variant = "alt"`. They never influence the decision; M9's tuning compares them. Cost about $0.17 per cycle.

## 2026-10-09 · M3 agents: model reads data, code scores

- Polymarket: the model only maps markets to (asset, threshold, direction) once a day; scoring is the implied median plus the 24h shift in code. Gamma `tag_id=21` (crypto) and `102000` (macro) with pagination; `tag_slug` is ignored by the API.
- Macro: the model returns regime, confidence and 48h event risk from z-scored JSON; coupling and the per-asset score are code. Gold from Bybit `XAUUSDT` (Stooq blocks scripts), stablecoin supply from DefiLlama, FOMC/CPI/jobs dates in `app/data/calendar_2026.toml`, ETF flows deferred (no free API).
- X: half of each cycle's read allowance to curated accounts, half to search; labels are a schema; aggregation shrinks toward a neutral prior and halves one-sided crowds.
- Chart patterns: disagreement between the code and vision tracks is penalized harder than a missing vision read (quarter vs half of the code score), so the vision model can only add conviction, never carry a trade alone.
- Shadow-mode provider swap is a config table (`[models.shadow_alt]`), not code per agent.

## 2026-10-09 · M8 split into a public site and a private admin (owner)

SPEC §12 is replaced. M8a is a static public site at dorkbot.dev (Cloudflare Pages) fed by a sanitized JSON snapshot pushed every 5 minutes; it shows performance, trades (after their X post), agent scores and a leaderboard, never amounts, balances, costs, IDs, keys, logs or raw X text. The snapshot is built from a pydantic whitelist and the X poster's number whitelist, with a test that fails on any forbidden field. M8b is a private admin at admin.dorkbot.dev served from dorkbot behind Cloudflare (443 from Cloudflare ranges only, SSH key only, no Tailscale): single user, argon2 password plus TOTP (re-asked for kill switch, resume and parameter changes), lockout, strict cookies, CSRF, email on new-IP login, parameter changes and kill-switch actions. The admin container holds no Bybit key; it writes requests to the database and the executor applies them within hard code bounds.
Config: `[site]` with the domains and `snapshot_interval_minutes = 5`. Open for M8b: the email provider for the alerts. Rules out: a dashboard reachable on the server without Cloudflare in front; any admin process holding trading secrets.

## 2026-10-09 · M2 indicators agent: rules fixed, no tuning on the backtest

The indicator rules (EMA stack scaled by ADX, MACD histogram over ATR, RSI momentum with contrarian extremes, OBV confirmation, funding crowding, OI confirmation; weights 0.30/0.20/0.15/0.15/0.10/0.10) are fixed in `app/agents/indicators.py` and were not tuned on the backtest. The backtest over the last 180 days of 4h bars shows no edge (IC ≈ 0, hit rate 42–50%). That is reported, not fixed: tuning rules on the same window would be curve-fitting, and SPEC §10 already handles a worthless agent by driving its weight to 0 during shadow mode. The harness exists so the question gets re-asked on new data every two weeks (SPEC §7 weight tuning).
Rules out: changing indicator rules without a fresh out-of-sample window to judge them on.

## 2026-10-09 · Candle backfill: 1,000 bars per pair and interval

While a (symbol, interval) has fewer than 1,000 stored bars the fetch asks Bybit for 1,000 (its maximum) instead of 200. That gives EMA 200 warm-up and a 160-day 4h backtest without a separate backfill tool. Upserts are chunked at 2,000 rows (Postgres bind-parameter limit).

## 2026-10-09 · Server: DigitalOcean droplet `dorkbot`, 164.90.211.109 (owner)

The bot will run on a DigitalOcean droplet named `dorkbot` in FRA1 (Frankfurt) with fixed IP 164.90.211.109; the owner's local SSH key is on it, so deployment runs from the owner's machine over SSH. Deployment is part of M6. The current Bybit OAuth key cannot be IP-bound; a web-created key bound to this IP replaces it before go-live.

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
