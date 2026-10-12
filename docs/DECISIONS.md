# Decision log

Newest first. One entry per decision: what was decided, why, and what it rules out.

## 2026-10-09 · M9 fixes: exits, backup stop, all-cost P&L, capital ramp, tuning, pilot, feed (owner)

Seven owner instructions after the M9 review. What the owner set is in SPEC §7.7, §9, §10 and §11; the choices below were mine.

- **Exits are IOC limits, not market orders.** 1% beyond the touch read at send time, at most five sends per call, and the executor calls again every pass until flat. Why: a spot market buy on Bybit is sized in quote coin by default, and a limit caps the damage on an empty book; the retry gets the rest. Rules out: a resting exit that the market can run away from.
- **A position is closed only when it is flat.** Partial exit fills accumulate on the row (`exit_filled_qty`, `exit_value`, `exit_fee`) and are booked once, at their average. Before this, an exit the venue refused was booked as closed at the quote. After three unfinished passes the owner gets an email.
- **An exit whose reply was lost is settled before anything else is sent.** The gateway remembers the `orderLinkId` and reads it back first, so a retry cannot sell twice. This memory is per process: after a restart in the middle of an unknown exit it is gone. Network failures now surface as `BybitTransportError` instead of escaping the gateway.
- **Backup stop: conditional `StopOrder`, market, no leverage.** `StopOrder` does not reserve the coins, so the bot's own exit can still use them. Without leverage a stale trigger can only fail; it cannot borrow and open a position. The executor cancels the backup before its own exit and does not send that exit while the cancel is unconfirmed: two sell orders for one position is the failure to avoid. Moving it is cancel-then-place, so there is never more than one resting; if the new one does not land, the position is without a backup until the next pass. When the plan carries no ATR, the stop distance stands in for it. An exchange-triggered fill is detected every full pass, booked as a stop, and emailed.
- **"All costs" is one function** (`app/costs.py`): LLM spend from `llm_calls`, X reads at $0.005 and posts at $0.015, server at $24 a month pro rata. USD costs are set against USDT P&L one for one. Shadow-mode costs include the second-provider runs, so the go-live test is stricter than live will be. Profit factor stays a per-trade figure.
- **Capital ramp "within limits"** means no drawdown pause and no emergency brake since the step began. `risk_state.capital_fraction` holds the step; `live_start_fraction` is the first one.
- **Tuning windows are 14 days**, matching the tuning interval; the ranking still uses the 90-day IC. An IC ≤ 0 without two negative windows leaves the weight unchanged, also when the ranking would have lowered it. Because every step still shrinks halfway toward equal weights, no weight reaches zero any more; SPEC §10's "weight goes to 0" now reads "is cut step by step".
- **Pilot runs beside the paper tracks.** Each position row carries its own mode and that picks the gateway, so a paper row can never reach the exchange. Before this, pilot mode stopped the paper tracks (pausing the shadow record) and would have sent real close orders for open paper positions.
- **WebSocket feed** on `orderbook.1.<symbol>`, default on, with the REST ticker as fallback when a quote is older than 10 s. Exits are checked every second on pushed quotes; controls, entries and accounting stay on the 10-second pass. `price_feed = "ws"` is a go-live criterion.

## 2026-10-09 · M9: the bot flips itself to live; pilot is a separate mode

`risk_state.mode` is set to live by the nightly go-live check when every §10 criterion holds, including the owner's 4-weeks-AND-100-trades rule and a passed live self-test; `live_allowed` in config is the owner's single switch and is one of the criteria, not a trigger. The executor decides its mode on start and each restart from that state. The pilot (`[pilot] enabled`) is a third mode with its own ledger (id 3) and `positions.mode = "pilot"`, so pilot fills, slippage and borrow behaviour are measured without touching the shadow statistics. Backups are nightly `pg_dump` files kept 14 days and mirrored to R2 once credentials exist.

## 2026-10-09 · M8: server-rendered admin, Cloudflare Tunnel, SSH-only firewall

The admin is FastAPI with Jinja templates rather than a separate Next.js app: one container, no build step, nothing the owner needs beyond a browser; a Next.js front can replace the templates later without touching auth or the data layer. The admin never gets the trading secrets: it runs from `.env.admin`, an allow-listed subset of `.env`, and a test enforces the list. Public exposure goes through a Cloudflare Tunnel (outbound only), so the droplet's firewall allows SSH and nothing else; SPEC §12's "443 only from Cloudflare ranges" is satisfied by having no 443 at all. Shadow posts' marker words and the site footer's "bot"/"nfa" are the only whitelist exceptions, and only in site mode.

## 2026-10-09 · Go-live after 4 weeks and 100 primary trades; execution pilot; post voice (owner)

- Shadow mode lasts at least 4 weeks AND until at least 100 trades have closed on the primary paper track, whichever comes later (previously 6 weeks). The other §10 criteria are unchanged. After the first 4 weeks the bot reports monthly running costs (LLM, X, server); the admin's cost page (M8b) carries it.
- After M9 an execution pilot runs 185 USDT (what the subaccount holds; spec said 200) at 1x on the real Bybit gateway to test fills, slippage and borrowing. Pilot trades are stored with `mode = "pilot"`, tracked separately, and never count toward go-live.
- Shadow posts carry a random test-mode marker ("dorking...", "just dorking", "still dorking around") instead of a "Paper:" prefix; `templates.enforce_marker` adds it to every shadow post and strips it from every live post, and the poster blocks a post whose marker state does not match its mode. The site keeps the shadow badge.
- X posts: normal capitalization, plain and direct; dry humour when natural. Supersedes "lowercase, nonchalant" in SPEC §11. Dry-run posts (shadow with `post_in_shadow = false`) are templates only, so no model spend on posts nobody sees.
- `dorkbot [HOURS]` zsh function on the owner's Mac runs `app.report` over SSH on the server: primary-track trades in the window, day and total P&L, LLM and X cost today.

## 2026-10-09 · M6 owner briefing: six execution rules

1. One PM flat and the other directional is no trade; the "partial" path (1x, no borrowing) from the M4 entry below is removed. Disagreement never trades; only conviction < 0.5 still means 1x without borrowing.
2. `capital_max_usdt = 10000` (renamed from `capital_max_usdc`): the paper equity in shadow mode. Live starts at `live_start_fraction = 0.10` of it and never sizes beyond the subaccount's actual equity.
3. Shadow runs two tracks. The primary (paper ledger 1, `positions.track = "primary"`) follows live rules: the leverage ceiling from `risk_state` (2x at the start, ramping per §8). The comparison track (ledger 2, `"max"`) uses `leverage_max`. Go-live criteria (M9) read the primary only.
4. Liquidation price follows Bybit's cross-margin rule per position: the coin side is haircut by its collateral ratio (from `/v5/spot-margin-trade/data`, stored every 15 minutes as `COLLATERAL_<coin>` alongside `BORROW_<coin>`), the liability carries the maintenance margin rate. The rate itself is not exposed by the API; `[risk] maintenance_margin_rate = 0.03` is the documented spot-margin figure and is confirmed in the live self-test (M9). Replaces the 1/L − 5% approximation in both the leverage buffer and the simulator.
5. Order books are stored at 200 levels with `depth_truncated` set when the snapshot ends inside ±2%. The risk engine records a `depth_truncated` flag on every plan sized from such a book and treats the depth as a lower bound.
6. `tests/test_executor_failures.py` covers partial fill, rejected order, feed drop during an active stop, restart mid-trade, failed borrow, gap through a stop and exchange unreachable, through a scripted order gateway against a dedicated `bot_test` database.

## 2026-10-09 · M5: shorts at 1x still borrow; Fear & Greed extreme caps leverage at 2x

A short on spot margin is always a borrow of the base coin, so the "no borrowing" rule (PMs disagree or conviction < 0.5) blocks every short, including at 1x, while a long at 1x proceeds without borrowing. The owner's Fear & Greed briefing asked for a risk flag for the leverage agent; it is implemented as a 2x cap when the index is above 80 or below 20, the same tier as a scheduled event. Shadow mode uses `leverage_max` as the ceiling so the full caps table gets exercised; the live ramp (2x → 5x → 10x) lives in `risk_state.leverage_ceiling` for M9.
Rules out: a short "without leverage" slipping past the disagreement rule.

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

## 2026-10-09 · A self-test kill closes real positions only

The live self-test's kill request (`source = live_selftest`) closes live and pilot positions and freezes the executor, but leaves the paper tracks' positions open. An admin kill still closes everything.
Why: the first full self-test closed all eight shadow positions four hours into the experiment. The shadow book is a running forward test; the self-test only needs to prove the kill path on the exchange. The eight positions closed with reason `kill` on 2026-10-09 15:19 UTC stay in the data as-is.

## 2026-10-09 · Spot-margin borrows are repaid explicitly

After a cover, the gateway calls `POST /v5/account/quick-repayment` for the base coin. Bybit UTA keeps the liability open after the coin is bought back; it settles on its own only later.

## 2026-10-09 · Polymarket: parser first, model second, every 15 minutes

Polymarket's crypto questions come from a handful of templates ("Will the price of Bitcoin be above $84,000 on October 9?", "Will Solana dip to $100 October 5-11?", "Bitcoin Up or Down - October 9, 4AM ET"). A regex parser maps them; the model only gets questions that mention one of our assets in a form the parser does not know. Mapping runs every 15 minutes right after the fetch, and at startup. Markets are fetched per Gamma event so ladders arrive whole; 5- and 15-minute Up/Down markets are not stored. Hourly, 4-hour and daily Up/Down markets add a momentum component (weight 0.25 next to shift 0.5 and level 0.25).
Why: the first day ran with zero mapped markets. The daily 02:30 UTC mapper never fired because the stack started after it, and every asset reported "no coverage" without saying why. The parser removes the model from the critical path and costs nothing.

## 2026-10-09 · Leverage caps editable up to 20x (SPX6900 10x)

The admin's parameter bounds allow `leverage_max` up to 20 and `leverage_max_spx6900` up to 10; the defaults in `config.toml` stay 10 and 3. Owner instruction. The other caps still apply on top: the leverage agent's liquidation-distance and depth rules, the live ramp (2x → 5x → 10x), the 2x cap after a drawdown pause, and what Bybit will actually lend per pair (the gateway sizes the borrow from `instruments-info` and the margin terms, so a 20x setting on a pair Bybit lends 10x on fills at what the exchange allows).

## 2026-10-09 · SPX6900 leans on X: per-asset weights and mention volume

Owner: for SPX6900, community sentiment and the volume of attention on X matter most. Three changes: (1) `[agents.weights_by_asset]` in config overrides the tuned weights per asset, renormalized over the valid agents; SPX6900 gets x_sentiment 0.40 and polymarket 0 (no markets on it). (2) The X agent counts mentions per asset every cycle with the counts endpoint (one request per asset, no post text, series `XMENTIONS_<asset>` in macro_observations) and scales its confidence by attention: twice the usual volume lifts it, half lowers it, SPX6900 twice as strongly; a spike (≥ 2×) or fade (≤ 0.5×) is a risk flag the PMs see. Direction still comes from sentiment only. (3) The PM prompt (v2) says to weigh x_sentiment first for SPX6900. @spx6900 joined the curated accounts. The per-asset ticker searches ($BTC, $ETH, $SOL, $BNB, $SPX/SPX6900) that already ran every cycle stay as they are.

## 2026-10-09 · HTTP/3 off for the zone (admin 403 from connection pooling)

The owner's browser got a bare "403 Forbidden / cloudflare" on admin.dorkbot.dev while incognito worked and a browser restart fixed it. Reproduced: a connection whose SNI is a Cloudflare Pages/R2 host (dorkbot.dev, www, data) reused for `Host: admin.dorkbot.dev` gets that 403 at the edge; the same reuse from a plain zone host (mail) works. HTTP/3 is switched off for the zone so browsers no longer pool the public site and the admin on one QUIC connection. If it recurs, the structural fix is to stop mixing Pages custom domains and tunnel hosts on one zone: serve the site as a Worker with static assets (plain zone host) or give the admin its own certificate (Advanced Certificate Manager).

## 2026-10-09 · Weekly improvement memo, emailed

Every Monday 06:00 UTC `app/memo.py` computes the week's facts in code (cycles and cost, trades per track, pilot-vs-paper slippage, agent/model leaderboard, weights, risk hits, posts, failed fetches, Polymarket coverage, X mention volume) and asks the memo model (`models.memo`, Opus) for three to six proposals, each tied to quoted numbers, with risk and effort. The memo is written to `logs/` and emailed through Resend. It never changes anything: the owner reviews it and changes are applied by hand with a version bump. Why: the bot's self-adjustment is deliberately limited to weight tuning and the ramps; everything structural stays a human decision, and the owner wants that decision prepared weekly.

## 2026-10-09 · Return goal: 1% per day, net; the max track runs the leveraged variant

The owner's target is an average of 1% per day on capital, net of every cost, to be approached by tuning parameters as the evidence comes in (weekly memo). Two things follow. (1) The weekly memo frames proposals against that goal and says plainly what the numbers support; the hard risk rules stay. (2) The **max** shadow track now runs the leveraged variant of the strategy: `trading.risk_per_trade_max` (default 2.5%, admin-editable 0.1–5%) sizes its positions instead of the primary's `risk_per_trade` (0.5%), so the paper record shows, side by side, the safe version that go-live is judged on and the "real 5x" version the goal needs. Primary sizing is unchanged.

## 2026-10-09 · Go-live starts a fresh ledger; the shadow ledgers freeze

In live mode the executor runs one track, `live`, on ledger 4, created on the first live tick with starting capital = the subaccount's real equity (latest account snapshot); `risk_state.starting_capital` is set to the same number so "% against starting capital" means real money. The paper tracks (primary, max) keep running beside live as the control group, on the same decisions, so the record shows live fills against the paper version at the live rules and against the leveraged version (`risk_per_trade_max`); that comparison is the evidence for stepping live risk up toward the owner's goal. Only the live track is posted to X and shown on the public site once live. The public site, the go-live checker, the report and the memo read `primary` before go-live and `live` after; shadow trades keep their paper badge on the site. Why: before this the live trades would have been booked onto the 10,000 USDT paper ledger, mixing simulated and real history and sizing day-loss and brake rules against paper numbers.

## 2026-10-09 · Leverage capped at 10x everywhere

Bybit spot margin lends up to 10x; the 20x admin bound only ever affected the paper max track. Owner: cap everything at 10x. `leverage_max = 10`, `leverage_max_spx6900 = 10`, admin bounds 1–10 for both.

## 2026-10-09 · The inverse track (for fun)

A fourth paper track, `inverse` (ledger 5), takes the opposite side of every primary decision with the stop and target mirrored around the entry, at the primary's size. Owner's idea, "just for fun"; it is also a clean control: if the bot has no edge, primary and inverse should look alike after costs; if it has the reverse of one, inverse wins. Never posted, never on the public site, shown on the admin's paper ledgers and in the memo.

## 2026-10-10 · PMs: Fable vs Astra; price table from third-party trackers

`pm_2` is `gpt-6-astra` (was GPT-5.6 Sol); the shadow-alt twin of `pm_1` is Astra too, so the leaderboard scores Fable and Astra in both seats. Astra passed a structured smoke call through `app/llm` (2.3 s). The `[llm.pricing]` rows for the GPT models come from third-party trackers (Astra $10/$50, Sol $5/$30, Terra $2/$12, Luna $0.20/$1.20 per 1M tokens), not from openai.com; the cost tiles are estimates until the owner confirms the published rates.

## 2026-10-10 · Stops against stop hunts: buffer, soft/hard stop, wick-outs (owner briefing)

1. The risk engine moves a PM's stop clear of levels: round numbers on a 1-2-5 grid of ~1.5% of price and the swing lows/highs of the last 48 hourly candles within ±8% of spot. A stop within `stop_buffer_atr` (0.5 ATR, in `risk_state`) of such a level moves beyond it by that buffer, repeated if it lands on the next level, never further than 3 ATR from the entry. Logged as a `stop_buffer` rule hit.
2. Every trade has two stops: the placed one is the **soft** stop and fires on a 15-minute candle close beyond it (the executor keeps the last mid before each 15-minute boundary as that candle's close); the **hard** stop sits `hard_stop_atr` (1 ATR) further and fires on touch. Position size is set by the hard stop, so a touch loses at most `risk_per_trade`. The exchange-side backup stop sits 0.5 ATR beyond the hard stop. Positions opened before this change have no hard stop and keep the old touch behaviour.
3. A stopped-out trade (`stop` or `stop_hard`) is checked once its planned holding window has ended: if the 15-minute candles after the stop-out reached the original target, it is a wick-out. The nightly go-live job classifies them; the admin shows stopped / wick-outs / rate per asset; the fortnightly tuning moves the buffer one 0.25 step up when the rate is above 40% and down when below 15%, within 0.25–1.5 ATR, with at least 20 stopped trades.

## 2026-10-10 · Hard stop at 0.75 ATR; hard-stop statistic

Senior-trader review of the stop-hunt changes: keep them; the hard stop goes from 1.0 to 0.75 ATR beyond the soft stop (less size penalty, most of the protection), set in `risk_state.hard_stop_atr`. The Stops panel also counts hard-stop exits per asset and how far past the soft stop they filled, so the gap can be judged in a fortnight. Owner: "don't be too careful".

## 2026-10-10 · Suppressed triggers are logged with the move that followed

When the 2/day cap blocks a trigger, the reason is logged (`suppressed_triggers`, once per hour per reason) with the asset's spot; an hourly job fills the price move 4 h and 1 d later. Shown on the decision log and summarized in the memo. Logging only: `check()` still returns None under the cap, the cap and the trigger thresholds are unchanged, and nothing reads the log for decisions. Owner: evaluate after the shadow period whether more triggered cycles are worth their cost.

## 2026-10-10 · Correlation-aware exposure cap (owner briefing)

Each asset gets a 30-day beta to BTC from daily returns (BTC = 1, 1 when fewer than ten matching days). Per track, net beta exposure = Σ sign × notional × beta over open positions; the risk engine caps it at `trading.net_beta_exposure_max` × equity (1.5 to start, admin-editable 0.5–5): a trade that adds to the net direction is sized into the room left, one against it is uncapped. Exposure (gross and net) is now computed per track, which also fixes the gross cap having counted every track's positions together. Betas and the per-track exposure are logged on the cycle row and shown on the admin overview ("Correlated exposure") and in the decision log header.

## 2026-10-10 · Logic v2: universe BTC, ETH, SOL, XRP, DOGE; depth-based leverage cap

Owner briefing. Verified first on Bybit: XRPUSDT and DOGEUSDT are Trading, UTA spot margin on, borrowable (max borrow 5.4 M XRP / 7 M DOGE at ~0.0063%/h), 24 h turnover 29.5 M / 6.4 M USDT (BNB: 1.8 M). Changes, as one logic version bump (`app/version.py`, stored on every cycle): assets BTC, ETH, SOL, XRP, DOGE; cashtags $XRP and $DOGE; Polymarket parser and mapper know XRP/Ripple and Dogecoin; X search and mention queries added; DOGE gets the memecoin treatment that SPX6900 had (count weight, attention swing, `weights_by_asset` x_sentiment 0.30, a PM note); the macro dominance tilt covers XRP (−1.0) and DOGE (−1.5). The SPX6900 leverage cap is replaced by a depth-based cap per asset: leverage such that capital_share × equity × L stays within `depth_cap` of the ±2% book, rounded down to a half, never below 1. Open BNB and SPX6900 positions (paper, and the pilot's BNB) are managed to their close: the executor quotes every symbol with an open position, not only the configured ones. History backfilled with `python -m app.data.backfill XRPUSDT DOGEUSDT`. The correlation-aware exposure cap ships in the same version.

## 2026-10-10 · Orak v3 (logic v3): distribution from range buckets, horizon weights, 4h shift

From the used-markets report: BTC's read was 55% driven by a market resolving the same afternoon, range-bucket events (a full histogram of the day's close) were thrown away, and the $1,000 volume cutoff dropped most of the ladder's tails and most of XRP/DOGE. Changes: (1) the "price on a date" buckets become cumulative P(≥ low edge) ladder points per date (three liquid buckets minimum); (2) the volume cutoff is $200 and volume weighting does the rest; (3) ladder points are weighted by horizon, daily/weekly/monthly 0.3/0.4/0.3 renormalized over the horizons present, volume-weighted within each; (4) the shift blends the 24h move (0.6) with a 4h move (0.4, 5 pp = ±1); (5) markets beyond 60 days are not scored but the two busiest appear as "long horizon" evidence lines for the PMs. Owner: "go".

## 2026-10-10 · Logic v4: two sleeves, frozen until the 4-week review (owner briefing)

Verified on Bybit first: PEPEUSDT, HBARUSDT, PUMPUSDT, SPXUSDT and ENAUSDT all trade with UTA spot margin and are borrowable (ENA passed, so NEAR is not used); SPX is the thinnest (0.7 M USDT/24 h, 21.6 k SPX max borrow). The book is split into **bluechips** (BTC, ETH, SOL, XRP, DOGE; 70% of capital; risk 1%, leverage ramp to 10x, the existing logic) and **alts** (PEPE, HBAR, PUMP, SPX6900, ENA; 30%; risk 0.5%, 3x, depth-based caps, Polymarket weight 0, X and chart weights 0.30 each, Sonnet for chart vision). Each sleeve has its own account view (its share of starting capital plus its realized and unrealized P&L), its own exposure cap, its own day-loss stop (locks only that sleeve until 00:00 UTC) and its own go-live evaluation (`risk_state.sleeve_modes`; the process goes live when the first sleeve does, the other keeps trading on paper). The beta-weighted cap applies per sleeve and across both together (`net_beta_total_max`). Betas now come from 4-hour returns over 30 days; the admin shows a 30/90-day beta, downside-beta and correlation table for all ten assets. LLM and X costs are reported per sleeve (calls tagged with an asset) plus a shared bucket (PM calls, account reads). The X search terms and cashtags, the Polymarket parser and the macro dominance tilt cover the five alts. BNB stays out. **The decision logic is frozen until the 4-week review on 2026-11-07**: the memo keeps proposing, nothing is applied before then.

## 2026-10-10 · Logic v5: Djaf, the catalyst agent (TypeSafe Jev) for the alt sleeve (owner briefing)

Alt sleeve only; bluechips unchanged. The Polymarket agent's weight in the alt sleeve (already 0) goes to a new **catalysts** agent, crew name Djaf. Sources: the official project accounts (pepecoineth, hedera, pumpfun, spx6900, ethena) and alt-news accounts (DefiLlama, Tokenomist_ai, cryptorank_io, Bybit_Official) on X, read every cycle within `catalysts.reads_per_cycle_max` of the daily X budget; Bybit announcements (public `/v5/announcements/index`, listings, delistings, maintenance, news) every 15 minutes; and the token-unlock calendar from **DefiLlama's public emissions datasets** (`defillama-datasets.llama.fi/emissions/<slug>`, free; the `api.llama.fi/emissions` endpoint went behind the paid plan) hourly, for ENA, PUMP and HBAR (PEPE and SPX6900 are fully circulating). Only text that names a sleeve asset reaches the classifier. **Jev classifies** each text event (asset as a Choice with a "none" option, type, direction, materiality as the probability of a > 3% move within 48 h); unlocks are structured, so code labels them (type unlock, bearish, materiality = share of supply released / 2%). **Code scores**: a decayed sum of sign × materiality × type weight, half-lives per type (12–48 h), unlocks ramping over the week before and fading three days after; events with materiality below 0.2 or an asset probability below 0.6 do not count. Jev also labels the alt sleeve's X posts (`[sleeves.alts].models.x_sentiment = "jev-latest"`; five questions per post, credibility as a four-level Score). Jev calls go through `app.llm.judge`: model from config, 20 s timeout, one retry, fail closed, logged to `llm_calls` as provider `typesafe` with tokens, cost ($0.042 per million input tokens, output free) and latency; the question set is versioned in `app/agents/jev_questions.py`. The key is `JEV_API_KEY`, scheduler and executor only (the admin env whitelist excludes it). **Shadow**: while the process is not live, claude-sonnet-5-5 labels the same events (`catalyst_events.shadow`) and posts (`x_post_labels`), the sleeve readings are also scored from its labels as variant `alt`, and the admin shows agreement and the IC of both plus Jev's cost and latency. Smoke test before wiring: one real call, 315 ms, 805 input tokens, $0.00003. The owner asked for this after the v4 freeze; it is a logic bump (v5) for the alt sleeve, the review date 2026-11-07 stands.


## 2026-10-10 · X search allowance: a third to accounts, the rest to search, rotating asset order

Found on the first v5 cycle: with ten assets, the per-cycle X allowance (90 reads, half to the curated accounts) was spent on the first four assets in config order, so the alts were never searched and their sentiment stayed empty. The accounts now get a third, search two thirds, and the asset order rotates by cycle slot so a short allowance reaches a different first asset each cycle. A fetch-allocation fix, not a decision-logic change.

## 2026-10-10 · Optima, the read-only cost and execution adviser (crew member 15, owner briefing)

Every Monday at 05:30 UTC, before Coach's memo, Optima computes in code: cost versus value per agent and per model this month (LLM cost, IC over 90 days, IC per dollar), the main labeller against its shadow (Jev vs Sonnet) and the PM seats against their alt variants; execution quality over the last 30 days per mode and sleeve (planned versus actual entry in basis points from the decision's plan, missed post-only and borrow-failed entries, fee drag as basis points of notional and as a share of gross P&L, depth usage as notional over the ±2% book at entry, real fills versus the simulator); and the month-end API spend forecast (month to date plus the last week's daily rate times the days left) against `budget.api_usd_per_month`. claude-opus-5-5 (`models.optima`) turns the facts into a validated `OptimaReport` (findings with area, severity and quoted evidence; proposals with the exact change, expected effect, cost delta and who decides: owner, review or none). The run is stored in `optima_runs`; a failed or invalid answer stores the facts with the error. Optima never changes config, models or code. Coach's memo gets Optima's report as its own section and the brief lists Optima's proposals as a separate checklist (memo prompt v4: do not repeat them). Admin only: the overview shows the latest run; the public site only gets Optima in the crew, never a cost. No logic version bump: nothing in the decision path changed.

## 2026-10-10 · Djaf: the project's own account counts less (owner)

The first catalyst rows were mostly the project accounts promoting their own coin. In the scoring (code only): an event from the asset's official account (`[catalysts.official]`, handle → asset) counts half when bullish and in full when bearish or neutral (self-reported bad news is the best source); its "partnership" and "other" posts need materiality ≥ 0.4 instead of 0.2; and only the two strongest events per account (or per other source) count in a window, so posting more does not score more. Owner: "go", with the review date unchanged.

## 2026-10-10 · A cut-off PM answer no longer fails the cycle; PM output room doubled

Cycle 14 failed: Fable in the alt seat hit the 4,000-token output limit, its JSON ended mid-string, and the SDK raised pydantic's ValidationError, which is not the LLM layer's error and escaped the per-seat fail-closed path. Now both providers turn parse failures and a `max_tokens` / incomplete stop into `LLMError` (one retry, then no proposals from that seat, logged as a failed call), `ask_pm` also catches validation errors as a second net, and `llm.max_output_tokens` goes from 4,000 to 8,000 (ten assets of proposals with reasons; the main Fable seat used 3,792). A single seat failing means no trade from that PM, never a failed cycle.


## 2026-10-10 · The curated X accounts are read through a ticker-filtered search; their news feeds Djaf (owner)

Found: the account reader walked the list from the top until its per-cycle allowance ran out, so 8 of the owner's 63 accounts were ever read, and three of Djaf's four news accounts never. Now X does the filtering: five search queries of the form `(from:a OR from:b ...) (BTC OR $BTC OR bitcoin ...) -is:retweet` cover all 67 third-party handles (the owner's list plus DefiLlama, Tokenomist_ai, cryptorank_io, Bybit_Official) within the 512-character query limit, with a since-marker so a post is billed once. Bare PUMP, SPX and ENA are not search terms (a word, an index, a fragment): only the cashtag or the project name. Timelines remain only for the five projects' own accounts. The posts feed Ears for all ten coins. For the alts, a third-party post that names a sleeve asset also becomes a catalyst event for Djaf at full weight (no extra reads); a third party's event of type "other" never counts, since an opinion is sentiment, not a catalyst. Djaf runs after Ears in the cycle so it sees the posts of the same cycle. Owner: "go", review date unchanged.

## 2026-10-10 · Server usage on the admin's System page, from DigitalOcean monitoring (owner)

The scheduler reads the droplet's monitoring metrics from the DigitalOcean API every 5 minutes (`[server] droplet_id`, token `DO_ACCESS_TOKEN`) and stores one row per sample in `server_metrics` (CPU busy % from the cumulative per-mode counters, memory used from `memory_available`, root-disk used, load 1/5/15, public bandwidth), kept 14 days. The admin's System page shows the latest sample, the hour's average, the 24-hour peak and a small chart per measure, amber and red above fixed thresholds. The token stays with the scheduler: the admin container reads the database only, and its env whitelist excludes the token (tested). First reading showed the disk at 81%, of which 58 GB was Docker build cache from a day of deploys: `scripts/deploy.sh` now prunes the build cache down to 4 GB after every build.

## 2026-10-10 · A charts-first score is logged beside the real one (owner; no logic change)

Owner: "show me the charts, I'll tell you the news"; X is full of shilling and made him wait too long as a trader. Rather than cut X's weight without evidence during the freeze, every cycle now stores `decisions.charts_first_score` beside `formula_score`: the same weighted formula, but X sentiment counts only when it points the same way as the charts (tape and chart patterns), nothing X-derived counts when the charts are flat, a bullish catalyst needs bullish charts, and a bearish catalyst still counts against a long. It decides nothing and feeds no manager. `app/decision/charts_first.py` compares the two per sleeve (how often X changed the call, IC of each, next-day return in the called direction); the admin's Readings page, Optima's facts and the memo carry it. The weight question is decided at the review on 2026-11-07 from these numbers. No logic version bump.

## 2026-10-11 · Solo calls are logged: what followed when one manager called and the other stayed flat (owner; no logic change)

Cycle 15: Fable proposed five shorts, Astra was flat on all ten; over six cycles Astra was flat in 98% of its proposals against Fable's 71%, in both seats. The two score the evidence almost alike; Astra wants confirmation before acting, and the prompt gives no number for "weak evidence". Since a trade needs both, Astra in effect vetoes, and a manager that is always flat cannot be ranked. `app/decision/solo_calls.py` now reports, over the main seats of the two seated models: solo calls per caller, the move in the called direction after 4 hours and a day (before costs), the share that went the caller's way, the agreed calls as a baseline, and each model's flat share. Shown on the admin's Readings page, in Optima's facts and in the memo. Whether to put the bar in code or change the second model is decided at the review on 2026-11-07. No logic version bump.

## 2026-10-11 · The managers' exact input is stored per cycle; a dump tool

Asked what Astra is sent: the evidence pack was not stored, only its length. Each cycle now keeps the exact pack both managers receive in `cycles.evidence_pack` (it holds labels and code-made summaries, never raw X text), the decision log shows it in a fold-out, and `python -m app.decision.pm_dump [cycle]` prints the system prompt, the user message and the logged calls; for cycles before this date it rebuilds the pack from the stored readings and says which figures are from today's candles. Found while reading it: the pack's `open_position` is always null and `day_pnl_pct` always 0, although the prompt says the managers see any open position. Left as is under the freeze and listed for the review.

## 2026-10-11 · A shadow third manager (Grok 4.7) and three decision rules compared (owner; no logic change)

Owner's idea: three managers, move on two out of three. Before changing the live two-of-two rule, a third manager runs in shadow: `models.pm_3_shadow = "grok-4.7"` (xAI's flagship, $2/$6 per million tokens, through xAI's OpenAI-compatible API with a pydantic response format; key `X_AI_API_KEY`, scheduler only). It gets the same evidence pack after the cycle's decisions are stored and sized, so it can neither delay nor change a trade; its proposals are stored as seat `pm_3`, variant `shadow`. A failure is logged and ignored. `app/decision/three_seat.py` judges every decision with all three answers by three rules: two of two (live), two of three with nobody opposite (three agreeing = full size, two = half), and a fixed threshold on the two seated managers' own scores (±0.10); per rule the trades it would have opened and the move after 4 hours and a day. Shown on the admin's Readings page, in Optima's facts and the memo; the model also appears on the leaderboard. The choice between the three rules is for the review on 2026-11-07. Smoke test: one real call, schema-valid, 6.6 s, $0.003.

## 2026-10-11 · Daerm; xAI schema without numeric bounds; Pedant off the site's crew

The shadow third manager is called **Daerm** (crew id `pm_3`), shown on the site's crew as a manager that only watches; Pedant is left off the site's crew for now (`public=False`) and stays in the admin and the docs. First real run of the seat failed validation: with numeric bounds in the JSON schema, xAI's constrained decoding mangled decimals (a score of -0.04 came out as -1.04, small positives as 0, stops as 0). The provider now sends xAI the schema with the bounds removed and validates the answer against the full pydantic schema afterwards, so the bounds still hold and a breach still fails closed. Grok 4.7 takes about 47 s at low and 115 s at medium reasoning effort on the managers' input; it runs at the bot's "medium" like the other two, with its own timeout (`llm.timeout_s_by_provider = { xai = 240 }`). About four cents per call.

## 2026-10-11 · Logic v6: three managers, two of three decide (owner, during the freeze)

Owner: "I want it for real." Dork (Astra) was flat in 96% of its proposals and, with two seats, held a veto: nothing had opened since cycle 9. Daerm (Grok 4.7, `models.pm_3`) takes a third seat. All three are asked in parallel with the same evidence pack. **Rule:** a trade needs at least two managers calling the same direction and nobody calling the opposite one. Everyone who answered agrees → agreement `agree`, full size. Two agree and the third is flat → `majority`: the risk engine halves the risk per trade (`majority_half_size` rule hit) and judges leverage and borrowing as for an agreement. One call alone → no trade; long against short → no trade. **Missing answers:** a manager that fails is left out and the other two must agree (the old rule); fewer than two answers → no trade, so one model alone never decides. The third seat has a deadline (`llm.pm_3_deadline_s`, 170 s; Grok needs 80 to 115 s at medium effort, its thinking, not the schema): past it Daerm counts as no answer. The merged plan is the conviction-weighted merge of the agreeing proposals; conviction is their minimum. `decisions.pm3_direction` stores the third call; PM prompt v6 says "one of three" and how the answers are used. The advice was to log two-of-three in shadow until the review; the owner chose to switch now. The old two-of-two outcome and a score-threshold rule stay logged beside the live rule (`app/decision/three_seat.py`) so the switch can be judged on 2026-11-07. Cost: about six cents more per cycle, and cycles take up to a minute longer.

## 2026-10-11 · Daerm runs at low reasoning effort

Measured on one evidence pack, Grok 4.7 through the API: low effort 47 to 52 s (about 3,200 reasoning tokens), medium 94 to 115 s and once over 150 s (7,400), high 182 s (14,300), xhigh 173 s (13,300). The time is the model thinking before it writes; with or without schema enforcement made no difference. Low and medium gave the same calls (one short, DOGE); high and xhigh added a second (XRP) with slightly better reward-to-risk. In the first dry run of v6 at medium, Daerm missed its 170 s deadline and the other two decided. A third seat has to arrive, so `llm.effort_by_provider = { xai = "low" }`; Fable and Astra stay at medium. Reasoning tokens are counted as output in the logged cost (about six cents per call at low).

## 2026-10-11 · Daerm at high reasoning effort (owner)

Owner's choice after the effort test: `llm.effort_by_provider = { xai = "high" }`. At high Grok 4.7 needs about three minutes on the managers' input (about 14,000 reasoning tokens, roughly twelve cents a call), so its timeout is 300 s and the third seat's deadline 320 s; a cycle now waits up to about five minutes for the third answer before the other two decide. In the test, high made one more call than low (XRP short beside DOGE) with slightly better reward-to-risk.

## 2026-10-11 · A deploy waits for a running cycle

A deploy at 02:49 UTC restarted the scheduler one minute into triggered cycle 17 and the cycle was lost; it was rerun by hand. `scripts/deploy.sh` now runs `python -m app.decision.idle` on the server after the build and before the restart: it waits (up to 15 minutes) while a cycle is running and in the 90 seconds before a scheduled one, and the deploy stops rather than interrupt.

## 2026-10-11 · Majority trades halve the margin, not the risk input; position size is already per sleeve

Fix: the half size for a two-of-three majority was applied to the risk-per-trade input, which only lowers the leverage and does nothing at 1x. It now halves the margin, like the existing half-risk rule, with the leverage worked out as for a full agreement. Checked while looking into why the paper ledger had no cash for a DOGE trade: since logic v4 a position's margin is 20% of its **sleeve's** equity, which is 14% of the book for a bluechip and 6% for an alt, so all ten assets fit at once. The five positions holding all the cash were opened on 9 October, before the sleeves, at 20% of the whole book; nothing needs changing, the cash frees as they close (by 12 October 15:20 UTC at the latest).

## 2026-10-11 · Logic v7: conviction bar for borrowing 0.5 → 0.4; Astra's prices confirmed; cached input priced

**Conviction bar (owner: "lower the bar").** A trade below the bar runs at 1x without borrowing, which rules out every short. The bar was 0.5 against the lowest conviction of the agreeing managers, and in the first two-of-three cycle both majority shorts stopped there (0.40 and 0.47). It is now 0.4 (`leverage.MIN_CONVICTION`); PM prompt v7 tells the managers what 0.4 means. **Astra's prices** are confirmed from the owner's OpenAI account: $10 input, $1 cached input, $50 output per million tokens. **Cost accounting:** `llm.pricing` takes an optional third rate for cached input, set for Astra, Grok ($0.50) and the Claude models (a tenth of input); `Usage.input_tokens` is now the whole input for every provider (the Anthropic provider adds cache reads and writes, which were not counted before), and the cached part is billed at its own rate. Cache writes are billed at the plain input rate, slightly under Anthropic's surcharge. Costs logged before today keep their old basis. The DigitalOcean token is read-only (owner) and all ten pairs are whitelisted on the Bybit key (owner; DOGE proven by a fill).

## 2026-10-11 · Deploy order when the config format changes; the idle check cannot block a fix

A new config format (three-part prices) was copied to the server before the code that reads it; the old code's idle check crashed on the config and the deploy skipped the restart, leaving the old containers with a config they could not reload for about two minutes (no errors were logged; the processes had loaded their config at start). Fixed by hand. From now on: code first, then the config, then the restart when a format changes; and the idle check exits 3 for "a cycle is running" (the deploy stops) while any other failure of the check lets the deploy proceed.

## 2026-10-11 · Daerm's deadline 320 s → 480 s

In cycle 19 Grok at high effort answered after 297 s, 23 s inside its deadline. The owner does not mind a cycle taking minutes, and a missed deadline silently turns two-of-three back into two-of-two, so the timeout is 450 s and the deadline 480 s.

## 2026-10-11 · Daerm: one try at high effort, then a quick try at low

Cycle 21 (12:02 UTC): Grok at high effort did not answer within 450 s and the cycle fell back to the other two managers; the earlier cycles took 175, 297 and 225 s. xAI itself was up. The seat now gets one try at high with a 320 s limit and, if that brings nothing, one try at low effort (`llm.pm_3_fallback_effort`, about 50 to 85 s; low and high made nearly the same calls in the effort test), all within the 480 s deadline. The decision log's manager calls show which effort answered. The first failed attempt used to vanish when the deadline cancelled it; each try is now logged as its own call.

## 2026-10-11 · What xAI's log showed; two bugs found in the pack it contained

The owner pasted xAI's console log of Daerm's failed call in cycle 21. It was logged at 12:11:08 UTC, the moment our 450 s timeout cut the connection, with an empty answer and only 2,260 reasoning tokens, where the successful calls produced 14,000 to 19,000 in three to five minutes. So the model was not thinking for long: the request was stalled or starved on xAI's side for most of the 450 s. It was billed about 4.3 cents all the same (the cost field confirms that reasoning tokens are billed as output and cached input at $0.50). The fallback added today (one try at high, then one at low) is the right remedy.

The log also contained the full evidence pack, which showed two bugs. (1) **The macro reading was invalid in most scheduled cycles**: macro data was fetched hourly at :05, cycles start at :02, so the data was 56 minutes old against a 30-minute validity limit; 80 of 140 macro readings were excluded, and it only counted when a restart had happened to refetch it. The macro fetch now runs at :25 and :55. (2) **PEPE's ATR reached the managers as 0.0**: the pack rounded the ATR to six decimals, and PEPE's is about 8e-08; it is now rounded to six significant digits. Both are fixes to what the managers were meant to see, not rule changes.

## 2026-10-11 · The decision log says why an agreed call did not trade

When the managers call a direction and nothing opens, the reason column now adds a short note. Blocked by the risk engine (`action = none`): the blocking rule in plain words, such as "conviction too low to borrow for a short" or "exposure cap is full". Approved but not opened by a ledger's executor: the executor records why per track in `decisions.exec_notes` ("out of funds", "already in a DOGE trade", "sleeve closed for the day", "rejected by the exchange: ..."), and the log shows "opened on pilot · out of funds (primary, inverse)". Decisions from before this change show the risk-rule reason, or "approved, not opened yet" where the executor's reason was not recorded.

## 2026-10-11 · The daily trade limit counts trades, not approvals

Found through the new notes in the decision log: `trades_per_day` (three per asset per day) counted decisions with an approved plan, whether or not anything opened. With the paper ledgers out of cash, three approvals without a single trade used up the day's limit for SOL, DOGE and ENA, and the extra cycle at 15:17 UTC was blocked by it. The count is now the positions actually opened today on the track being sized.

## 2026-10-11 · Posts are labelled fresh or recap (logged only)

Owner: a post such as "500M outflows last week" is labelled bearish, correctly for the text, while the market may have turned since. Every labelled post now also carries `fresh`: true for something that happened or was announced within about a day or a new forward-looking view, false for a recap of older, already known information. The Sonnet labeller gets the field in its prompt (v4), Jev a sixth question per post (question set v2). The score does **not** use it: `aggregate` is unchanged and the field never reaches the managers' evidence lines. The admin's market-sentiment table shows the recaps per asset (bullish / bearish) and "Ears, fresh only", the score without them, so the review can judge whether recaps should count less or posts should decay with age.

## 2026-10-11 · A close post replies to the opener that went out

The BTC close post of 16:06 UTC was never queued: the position had two "open" post rows (a draft blocked by the whitelist and the retry that was posted), and the lookup for the opener to reply to demanded exactly one. It now takes the latest opener that has an X id. The missing close post was queued by hand afterwards.

## 2026-10-11 · Logic v8: the day-loss stop locks only the losing direction (owner)

At 16:02 UTC the bluechip sleeve hit -2.21% for the day; its shorts were closed and the sleeve was locked until midnight, in the same cycle in which all three managers turned long on BTC. Owner: a bot is not emotional, so why a cooling-off? The stop has two halves. **Closing the sleeve at the limit** is risk control and stays: correlated positions, a view that is wrong, or a fault all argue for getting flat. **Locking the sleeve until midnight** is the half borrowed from human discipline; for a bot its real use is stopping the loop of re-entering the same losing idea every cycle. So from v8 the lock applies only to the direction(s) that lost money today (`sleeves.losing_directions`; both when that cannot be told), and trades the other way are allowed. To keep the stop from firing again on the same realized loss, an active lock re-triggers only when the sleeve loses the stop's size again beyond its level at the lock, and then locks both directions. The executor closes the positions that were open when the stop fired (`since`), not ones opened afterwards. `risk_state.day_locked_sleeves` now holds `{sleeve: {until, dirs, since, pnl_at_lock}}`; the old bare `until` string still reads as both directions. Today's bluechip lock was converted to shorts only.

## 2026-10-11 · Aggressive mode: a switch on the parameters page (owner)

Owner: a switch that pushes leverage and risk in pursuit of the 1%-a-day goal; no trade held longer than 24 hours; **no locks** while it is on ("it keeps going and pushing until liquidated"); and a trade is **always closed once it is between 1 and 2% up**. The switch is `risk_state.aggressive`, set from the admin's Parameters page behind the step-up code, audited and mailed; the profile is `[aggressive]` in config.toml and applies from the next cycle.

What it changes: risk per trade × 2.5 per sleeve (bluechips 1% → 2.5%, alts 0.5% → 1.25%); the full leverage ceiling of 10x at once instead of the 2x start ceiling (a sleeve's own cap still holds: alts 3x); exposure cap 3× equity; holding limit 24 hours, capped in code and told to the managers through the pack's `max_hold_hours` (PM prompt v8). **No locks:** the sleeve day-loss stop and its direction lock, the account day-loss stop, the drawdown pause, the automatic brake on a deep loss or on repeated pauses, half risk after a pause, the leverage cut after losing days and the limit of three trades per asset per day are all off; any existing lock is cleared. **Profit taking:** the executor closes a position (reason `take_profit`) once its net unrealized profit reaches `take_profit_equity_pct` (1%) of its ledger's equity; with a ten-second check it fills at or a little above that. *Assumption:* the "1 to 2%" is measured on the ledger's equity, the same base as the 1%-a-day goal, not on the price move or on the margin; on margin it would be a tenth of a percent of price at 10x and lose to the fees.

What still protects the account: each trade's own soft and hard stop (a plan cannot be sized without one), the liquidation guard, the exchange-side backup stop on real positions, the kill switch, and a brake set by hand or by a balance mismatch (non-negotiable 8). Hard bounds in code: risk per trade ≤ 5%, leverage ≤ 10x. Not changed: the pilot (1x at its fixed size), open positions' stops and time limits, and, once live, the go-live leverage ramp (2x, 5x, 10x). Each cycle records whether the switch was on (`cycles.aggressive`); with it off the logic is unchanged, so no logic version bump. The mode aims at the goal and does not guarantee it; without a loss lock a losing run is not stopped and a ledger can lose most of its value. The owner was told and chose it.

## 2026-10-11 · Aggressive mode's profit level is net of every cost

Owner: "1-2% net profit". The take-profit check now also subtracts the fee for closing, so the profit banked, after entry fee, exit fee and borrow interest, is at least 1% of the ledger's equity.

## 2026-10-11 · Closing positions by hand without the kill switch

Owner asked to close every open trade (18:27 UTC: SPX on three paper ledgers, BNB on the inverse ledger and the pilot's real BNB short). The kill switch would also freeze the bot, so the positions were set to `closing` with the new exit reason `manual`; the executor then closes each through its normal exit path (cancel the backup stop, exit order, repay, book the result). Only the executor places the orders.

## 2026-10-11 · The executor cancels backup stops that no position owns

After the manual close, a buy stop for 431.6 DOGE was still resting on Bybit, trigger 0.09049, for a short the sleeve stop had closed at 16:06 UTC; the position's record held no link to it. A placement had landed on the exchange while the call reported a failure, so the bot never tracked it and placed another. Had price reached the trigger it would have bought DOGE with nothing to cover. Once a minute the executor now lists its backup stops on the exchange (`bk-` links) and cancels any that no open position owns, after seeing it unowned on two rounds in a row so a stop placed that minute is not hit; each cancel is logged and mailed.

## 2026-10-11 · Aggressive mode: stops with more room

Owner: his own stops are mostly further away. In aggressive mode every stop is moved `stop_room` (1.5) times further from the middle of the entry zone before the risk engine sees the plan; the stop-hunt buffer and the hard stop are then applied as usual, and the trade is sized on the new distance, so the position is smaller and the loss at the stop is unchanged. Never more than three times further, and never past the distance at which the position would reach 1x leverage (a position cannot shrink below that, so more room there would mean a larger loss); a stop already that far away is left where it is. Context given to the owner: in this mode a winner banks about 1% of the book and a stopped bluechip trade loses about 1.75%, so it needs clearly more than six winners in ten; more room raises the share of winners and asks a larger move for the same 1%.

## 2026-10-11 · The pilot has room for every trade

The pilot followed every approved plan but sized each position at 20% of its 185 USDT, so five positions filled it and the rest were skipped as "out of funds". Its slot is now a share of the asset's sleeve, like the paper book: 14% of the pilot's capital for a bluechip (about 26 USDT) and 6% for an alt (about 11 USDT), so all ten can be open at once. Still 1x and still the pilot's own fixed capital, also in aggressive mode: its job is to test fills, slippage and borrowing with real money, not to chase the return.


## 2026-10-12 · Live actuals: open trades with the price now, every 15 seconds

Owner: the site should show the live figures from the backend, not a five-minute-old picture. Until now an open trade's result was computed from the last 15-minute candle close and published with the five-minute snapshot, so it could be twenty minutes behind.

- The executor writes its latest quote per symbol to a new `marks` table on every pass (about ten seconds), stamped with the quote's own time. Nothing decides on this table; it only feeds displays.
- "Price now" is the mid of that quote when it is at most two minutes old, else the last 15-minute close (the executor is down or the feed is stale).
- A scheduler job publishes `live.json` to R2 every `site.live_interval_seconds` (15): mode plus the open trades of the storyline track with price now and the result in price and on margin. It passes the same whitelist as the snapshot (no sizes, no amounts) and is cached for five seconds. With nothing open and nothing changed it writes at most once per five minutes.
- The site polls it every 15 seconds while the tab is visible and uses it for the open-trades table whenever it is newer than the snapshot; the table gained a "now" column. Everything else on the site stays on the five-minute snapshot.
- The admin's open-positions table shows price now, result in price and on margin, and the result in USDT before fees, for all tracks, and refreshes itself every 15 seconds.

## 2026-10-12 · Aggressive mode: the 1% is a day goal for the whole ledger

Owner: "the goal is to let the bot generate 1% net profit per day on the entire wallet." The profit rule of 2026-10-11 closed one trade once that trade alone had made 1% of the ledger's equity. With a slot of 14% of the book that needs a price move of 2.3% at 6.9x, further than most targets, so the rule almost never fired, and several trades that made 1% together were left to run.

- The executor now values each ledger on every pass: cash plus every open trade at the price now, minus the fee for closing each of them. When that stands `take_profit_equity_pct` (1%) above the base, every open trade on that ledger is closed with reason `take_profit`.
- The base is the day's start (00:00 UTC). When the goal is reached it moves up to the banked equity (`paper_account.goal_base`), so the next 1% counts from there. The owner asked for no locks in this mode: the bot keeps trading after a banked goal instead of stopping for the day.
- A ledger with nothing open that already stands a goal above its base (a target or a single trade banked it) is rebased the same way, so the next trade is not closed the moment it opens.
- The single-trade rule stays: one trade that makes the goal alone is closed. It is now a special case of the day goal.
- Each ledger (main, max, inverse, pilot) has its own goal. Losses are not part of this rule: a ledger that is down 2% needs to come back to +1% on the day before anything is banked.
- No logic version bump, like the rest of aggressive mode: it is an exit rule behind the owner's switch.

## 2026-10-12 · Aggressive mode: a banked day goal starts a new decision cycle

Owner: when the 1% is banked and the next cycle is hours away, keep going. Banking closes every trade on the ledger, so without this the book sat in cash until the next four-hour cycle.

- When the main ledger (`primary`, or `live` once live) banks its goal, the executor leaves a request in `control_requests` (kind `cycle`). It places no orders for it and decides nothing: the request only asks the scheduler for a normal decision cycle.
- The scheduler checks every 30 seconds and starts a full cycle (all agents, three managers, risk engine), recorded with kind `goal`. That kind does not count toward the two-a-day cap on market triggers.
- No extra cycle when the regular one starts within `aggressive.goal_cycle_min_gap_minutes` (30), when a cycle is already running, or when aggressive mode has been switched off. A skipped request is closed with the reason, not carried over.
- The other ledgers (max, inverse, pilot) bank their own goal but do not start cycles: one set of decisions serves all of them, and the main ledger is the record.
- Each extra cycle costs what a regular one does in model and X usage. It needs another 1% on top of the banked level before the next one, so it cannot loop.
- `aggressive.goal_cycle = false` switches it off.

## 2026-10-12 · Aggressive mode: holding limit 48 hours instead of 24

Owner: the 24-hour limit was wishful thinking. Targets come off the 4-hour chart and stops have 1.5 times more room in this mode, so most trades would reach neither end within a day and be closed in the middle with both fees paid. The day goal (1% on the ledger) now banks profit, so the limit only has to cut trades that go nowhere. `aggressive.max_hold_hours` is 48. It applies to trades opened from now on: the limit is stored on each position at entry, and the four trades open at the change keep their 24 hours.
