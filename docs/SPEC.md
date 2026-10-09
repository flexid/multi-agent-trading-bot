# Multi-agent trading bot: build spec

Owner: Lex. Status: plan approved, ready to build.

> 2026-10-09: two owner changes override the text below. No Telegram: every mention of it is dropped, the dashboard carries notifications and the kill switch. Quote coin is USDT: read every `…USDC` symbol as `…USDT`. See `docs/DECISIONS.md`.

## 1. What it is

A fully autonomous trader on Bybit EU for five assets: BTC, ETH, SOL, BNB and SPX6900. Every 4 hours, five analysis agents score each asset. Two LLM portfolio managers (Claude and GPT) independently propose trades. A leverage agent sizes them between 1x and 10x. A deterministic risk engine decides, and a separate executor places and manages the orders. Every executed trade is posted to X (@decentradork) in casual, human language.

The owner sets parameters once and never reviews or approves anything. The only human touchpoint is the emergency brake (§8).

## 2. Hard rules

See `CLAUDE.md` › Non-negotiables. They apply to every milestone.

## 3. Exchange constraints (Bybit EU)

> 2026-10-09: the owner moved the bot to Bybit global (`https://api.bybit.com`). Host and per-pair facts below are superseded by `docs/DECISIONS.md` and `docs/M0_REPORT.md`; the spot-and-spot-margin scope stands.

- REST base `https://api.bybit.eu`, V5 API. Requests from US IPs return 403, so the server runs in Frankfurt or Amsterdam.
- Products: spot plus spot margin up to 10x, cross margin only. No perpetuals, no TradFi.
- Symbols against USDC: `BTCUSDC`, `ETHUSDC`, `SOLUSDC`, `BNBUSDC`, `SPXUSDC` (SPX6900, listing to be verified in M0).
- Shorting means borrowing the coin on spot margin and selling it. Verify per pair; where unavailable, a negative signal means flat.
- Some books are thin: BNB/USDC trades roughly $0.1M a day against roughly $3M for SOL/USDC. Cap every position at 5% of order-book depth within ±2%.
- Bybit global public market data (perp funding rates, open interest) can be read from `https://api.bybit.com` without auth, as indicator input only.
- Keys without an IP whitelist expire after 3 months (documented for Bybit global). Bind the key to the server IP.
- Check whether `pybit` supports the EU host. If not, write a thin HMAC-signed client following the V5 auth docs.

## 4. Architecture

```
data layer (15 min) → 5 agents → PM 1 + PM 2 → leverage agent → risk engine → executor
                                                                            → X poster
                                                                            → dashboard / Telegram
```

Suggested layout:

```
app/
  config.py        # config.toml + .env, typed
  db/              # SQLAlchemy models, Alembic migrations
  data/            # fetchers: bybit, polymarket, x, macro
  agents/          # macro, chart_patterns, indicators, polymarket, x_sentiment
  llm/             # provider-agnostic client, structured output, cost tracking
  prompts/         # versioned prompt files
  decision/        # evidence pack, PMs, consensus, formula anchor, weight tuning
  risk/            # leverage agent, risk engine, limits, go-live checker
  execution/       # bybit client, executor process, paper simulator, reconciliation
  social/          # x poster: writer, auditor, whitelist, templates, scheduling
  notify/          # telegram
  scheduler.py     # 15-min data, 4-h cycles, triggers, daily report
api/               # FastAPI for the dashboard
web/               # Next.js dashboard
tests/
docker-compose.yml
config.toml
```

## 5. Data layer (every 15 minutes)

- **Bybit:** OHLCV 15m / 1h / 4h / 1D for the five pairs, order-book depth, own balances, borrows and open orders.
- **Polymarket** (public, no key): Gamma `https://gamma-api.polymarket.com` for discovery, CLOB `https://clob.polymarket.com` for prices, history and books, Data `https://data-api.polymarket.com`. Gotcha: Gamma returns `outcomePrices`, `outcomes` and `clobTokenIds` as JSON strings inside the JSON.
- **X API** (pay-per-use): at most 600 post reads per day. $0.005 per post read; search only covers the last 7 days.
- **Macro:** FRED (yields, dollar index, VIX), S&P 500, Nasdaq and gold prices, economic calendar (FOMC, CPI, jobs report), spot ETF flows, stablecoin supply. Pick free or cheap sources for the last three and record the choice in `docs/DECISIONS.md`.

## 6. Agents

All five run every 4 hours, plus triggered cycles (§7). Output per agent per asset:

```json
{
  "agent": "polymarket",
  "asset": "BTC",
  "score": 0.35,
  "confidence": 0.6,
  "horizon": "1-3d",
  "evidence": ["P(BTC above threshold at week end) 62%, +9 pp in 24h"],
  "risk_flags": ["FOMC within 24h"],
  "data_age_min": 12
}
```

`score` is in [-1, 1], `confidence` in [0, 1]. Inputs older than 30 minutes mark the output invalid.

| Agent | Method | Model |
| --- | --- | --- |
| Macro | Code normalizes the data; the model returns a regime (risk-on, neutral, risk-off) and 48-hour event risk. Code computes a per-asset coupling score (0–1) from rolling 7-day and 30-day correlations with S&P 500, Nasdaq, dollar index and gold. Crypto can decouple from or move against tradfi: negative correlation counts as decoupled (coupling 0), never as an inverted signal. Low coupling scales the macro weight down; crypto-native drivers (ETF flows, stablecoin supply, funding, hacks, listings, unlocks) take over. | GPT-5.6 Terra |
| Chart patterns | Two tracks. Code detects levels, trendlines, breakouts and ranges. A vision model reads charts rendered with mplfinance in a fixed style (1h, 4h, 1D). Full weight only when both tracks agree. | `claude-opus-5-5` |
| Indicators | Fully deterministic: EMA 20/50/200, ADX, RSI, MACD, ATR, realized vol, OBV, funding, OI change, each mapped to a score by fixed rules. The model only summarizes. Must be backtestable. | code + `claude-haiku-5-5` |
| Polymarket | Fetch whole crypto events (a threshold ladder is one event). Never store 5- and 15-minute markets; ignore low-volume markets and markets less than an hour from resolution. Build an implied distribution from threshold ladders; the 24-hour change weighs more than the level; hourly, 4-hour and daily Up/Down markets add a momentum read. BNB and SPX6900 have few or no markets, so this agent weighs less there. S&P 500 daily markets feed the macro agent. A deterministic parser maps new markets every 15 minutes and at startup; the model only sees questions the parser cannot read. The health page tells "no markets" from "markets, none usable". | code + GPT-5.6 Luna |
| X sentiment | A curated list of 50–100 accounts plus per-asset search. The model labels each post: asset, stance, type (news, analysis, shill), credibility. Code aggregates into a sentiment level and a news-shock flag. Extreme one-sided sentiment counts as contrarian caution. Weighs heavier for SPX6900. | `claude-sonnet-5-5`  Mention volume per asset (counts endpoint) scales confidence; SPX6900 leans on this agent via a per-asset weight override. |

During shadow mode every agent also runs on the other provider. Per task, keep the model that measurably scores better.

## 7. Decision layer

1. **Evidence pack:** the five agent outputs per asset, open positions, P&L for the day, the last 10 decisions with outcomes, macro regime and coupling.
2. **Two PMs:** PM 1 = `claude-fable-5-1`, PM 2 = GPT-5.6 Sol. Same pack, no tools, no internet, neither sees the other's answer. Output per asset: direction (long, flat, short), entry zone, stop, target, maximum holding time (1 hour to 7 days), score, conviction, top three reasons, and which agents they weighted up or down.
3. **Formula anchor:** a weighted sum of agent scores. Start weights: indicators 0.25, chart patterns 0.20, Polymarket 0.20, macro 0.20 × coupling, X 0.15, renormalized to 1.
4. **Consensus:** same direction gives the conviction-weighted mean of both scores. Opposite directions, or one PM flat while the other is directional, mean no new trade for that asset (owner, 2026-10-09). Any failure means no new trade. Consensus is clamped to formula score ± 0.4.
5. **Triggered cycles** (max 2 per day): price move above 2× ATR within an hour, a Polymarket probability shift above 10 percentage points, or an X news shock.
6. **Open positions** are re-assessed every cycle: hold, adjust stop, or close.
7. **Weight auto-tuning** every 2 weeks: only after at least 100 closed trades, at most ±5 percentage points per agent per step, then shrink halfway toward equal weights. Measure each agent and each model by the correlation of its score with forward returns over 4 hours, 1 day and 3 days (IC). An agent with IC ≤ 0 is never set to zero in one step: it loses at most 5 percentage points per tuning step, and only when its IC was negative in each of the last two consecutive 14-day windows; otherwise its weight stands (owner, 2026-10-09).

## 8. Leverage agent and risk engine

Base leverage:

```
L = min(10, r / (a * d))
r = risk per trade (0.5% of equity)
a = capital share per asset (20%)
d = stop distance as a fraction (ATR-based)
```

A 1% stop gives 2.5x, a 0.5% stop gives 5x. Then the lowest of these applies:

| Situation | Max leverage |
| --- | --- |
| Absolute cap (`leverage_max`) | 10x by default; the owner can set it from the admin up to 20x |
| SPX6900 (`leverage_max_spx6900`) | 3x by default; settable up to 10x |
| ATR above its 30-day 90th percentile | half the computed value |
| FOMC, CPI or jobs report today | 2x |
| Risk-off regime while coupling > 0.5 | 2x |
| Conviction < 0.5 (disagreement is already no trade) | 1x, no borrowing |
| Two losing days in a row | half the computed value |
| Liquidation price closer than 3× the stop distance (Bybit cross-margin rule: collateral ratios from the API, maintenance margin rate) | reduce until the buffer holds |
| Gross exposure across all positions | at most 3× equity |
| Position above 5% of ±2% book depth (200-level books; a truncated book is flagged and its depth treated as a lower bound) | shrink below that |

An LLM may flag risks that are not in the numbers (thin books, news). It may only lower leverage, never raise it.

The leverage ceiling ramps itself: live starts at 2x, 5x after 4 weeks within all limits, 10x after 3 months net-positive after all costs (§10). It drops back automatically on underperformance.

Cost rule: take a trade only if the target covers at least 3× fees plus borrow interest at the chosen leverage.

Limits:

- Day loss of −2%: close everything, no new trades until 00:00 UTC.
- From +1.5% on the day: move stops so the day closes at +0.75% or better.
- At most 3 trades per asset per day.
- Drawdown of −10% from peak: close everything, pause 72 hours, restart with half risk and a 2x leverage cap.
- Emergency brake: −25% against starting capital, or 2 pauses within 30 days. Stop permanently, notify the owner, wait for him.
- Fewer than 3 valid agents: no new trades.

## 9. Execution

- Separate process. Idempotent `orderLinkId`. Prices from the public WebSocket (`orderbook.1`), with the REST ticker as fallback when the stream goes quiet; on the stream, exits are checked every second.
- **Entries** are post-only limit orders at the touch, repriced once after 2 minutes.
- **Exits are never post-only** (owner, 2026-10-09). Stop, target, trailing stop, time-stop, liquidation guard, risk close-all and kill all leave as an IOC limit priced about 1% beyond the touch, re-sent at the new touch until the position is flat. A position is only booked as closed once it is flat.
- Stops, targets, trailing stops and time-stops are enforced in code.
- **Exchange-side backup stop** (owner, 2026-10-09): every real entry also places a conditional stop-market order on Bybit, 0.5 ATR(14, 4h) beyond the bot's own stop, in case the executor or its connection dies. It moves with every change of the bot's stop and is cancelled before the bot's own exit goes out. `[exchange] backup_stop` turns it off if spot margin turns out not to support it; the live self-test verifies it.
- Paper-fill simulator for shadow mode: fills at bid or ask, fees, borrow interest at the exchange's hourly rate, leverage and liquidation price modeled with Bybit's collateral ratios.
- Two shadow tracks: the primary follows live rules (2x leverage ceiling at the start, ramping per §8) and is what the go-live criteria judge; a second track at `leverage_max` runs for comparison only.
- Before shadow mode starts, tests must pass for: partial fill, rejected order, price-feed drop during an active stop, restart mid-trade, failed borrow, gap through a stop, exchange unreachable.
- Reconciliation every cycle against real balances and borrows.
- Kill switch from the dashboard and a Telegram command: close all, repay margin, freeze. Optional; the bot never needs it.

## 10. Shadow mode and automatic go-live

The bot goes live by itself when `live_allowed = true` and all of these hold:

- At least 4 weeks of shadow mode AND at least 100 closed trades on the primary paper track, whichever comes later (owner, 2026-10-09; was 6 weeks); the last 2 weeks without operational incidents
- Net positive after ALL costs: fees, borrow interest, LLM, X and server (owner, 2026-10-09); profit factor ≥ 1.3
- Sharpe ratio above BTC buy-and-hold over the same period
- Max drawdown below 10%, zero simulated liquidations
- Each agent shows added value, or its weight is cut step by step (§7.7)
- Self-test on Bybit with minimal size: order, backup stop, margin borrow, close, kill switch
- The executor's price feed is the public WebSocket, not the 10-second REST poll (not required for the pilot)

Live starts with 10% of `capital_max_usdt` and max 2x. Leverage then scales per §8.

**Capital ramp** (owner, 2026-10-09): 10% → 25% → 50% → 100% of `capital_max_usdt`. Each step up needs 3 weeks at the current step within limits (no drawdown pause, no emergency brake) and net positive after all costs over those weeks. A drawdown pause moves it one step back, never below 10%. Every move restarts the 3-week clock. Position sizing never exceeds actual subaccount equity.

After the first 4 weeks of shadow mode the bot reports monthly running costs (LLM, X, server).

**Execution pilot (after M9):** 200 USDT at 1x on the real Bybit gateway to test fills, slippage and borrowing. Pilot results are tracked separately (`positions.mode = "pilot"`) and do not count toward the go-live criteria. The paper tracks keep running next to the pilot. Pilot trades are never posted to X: the primary paper track stays the X storyline until go-live (owner, 2026-10-09).

Backtesting: only the indicator agent and the risk engine on historical candles. Backtests with LLMs in the loop suffer look-ahead (the models know how history played out), so forward testing in shadow mode is the real test.

## 11. X poster (@decentradork)

- Post only after a fill, never a planned trade. Opening = new post; close = reply in the same thread. Only primary-track trades are posted; pilot trades never are.
- Claude writes, GPT audits, as in the owner's retweet-mirror project. The writer only sees the trade record and summarized PM reasons, never raw X content.
- Always the cashtag: `$BTC`, `$ETH`, `$SOL`, `$BNB`, `$SPX` (for SPX6900).
- Opening: cashtag, direction, entry, leverage, stop, target, usually a short reason. Close: cashtag, exit, % on price and on margin, holding time.
- Never amounts: no position size, no dollar or euro P&L, no balance. Code whitelist: only prices, leverage, percentages and durations may appear as numbers. Anything else fails the audit.
- Tone: casual, nonchalant, lowercase is fine, rare emoji, no hashtags, no links (a post with a URL costs $0.20 instead of $0.015), no mention of automation, no advice language ("buy", price promises).
- Style profile from about 200 recent own posts (owned reads); check against the archive so no sentence repeats.
- Random delay of 1 to 10 minutes after the fill. Above `max_posts_per_day`, bundle closes into one daily summary.
- Audit fails: fall back to one of about 20 casual templates filled from the trade log.
- Shadow mode posts nothing unless `post_in_shadow = true`; then every post carries a casual test-mode marker picked at random ("dorking...", "just dorking", "still dorking around"), enforced in code; no live post ever carries one (owner, 2026-10-09). The site shows the shadow badge.
- Own copy of the existing X keys. No shared database with retweet-mirror.

Examples:

> took a $SOL long at 74.10, 3x. range finally broke and polymarket odds on a green week went from 48 to 61%. stop 72.30, target 78

> closed $SOL at 77.60 after 9 hours. +4.7% on price, +13.6% on margin. ran out of steam just before target, i'll take it

## 12. Public site and private admin (M8, replaces the earlier dashboard section)

Two separate parts.

### M8a: public site, dorkbot.dev

Purpose: dorkbot is openly a bot. The site shows how it trades. It links to @decentradork, and the X bio links back. X posts themselves never contain links.

Hosting

- Static site (Next.js static export) on Cloudflare Pages.
- Data comes from a sanitized JSON snapshot that dorkbot pushes every 5 minutes to Cloudflare R2 (or a Pages deploy). Nothing on dorkbot is reachable for this.
- Auto-generated Open Graph image with current performance, so the link renders well on X.

Content

- Performance in % against BTC buy-and-hold and an equal-weight basket, plus drawdown.
- Stats: number of trades, win rate, profit factor, average holding time.
- Open trades: asset, direction, entry, leverage, stop, target, time in trade, unrealized % on price and on margin. A trade appears only after its X post.
- Closed trades: entry, exit, leverage, % on price, % on margin, holding time, and a link to its X thread.
- Agents: latest score per agent per asset, Claude against GPT, consensus, and short reasons for entering or staying out. Macro regime and tradfi coupling per asset, plus the crypto-native inputs (Fear & Greed, BTC dominance) and the macro agent's tradfi and native components.
- Leaderboard: which agents and models have measurable value so far.
- Mode badge: shadow or live. Paper trades are labelled paper.

Never on the public site: amounts, balances, position sizes, costs, API spend, account or subaccount IDs, keys, logs, and raw X posts from other accounts (only labels and summaries).

Safety

- The snapshot is built from an explicit pydantic whitelist schema, never by dumping database rows.
- Numbers pass the same whitelist as the X poster: prices, leverage, percentages and durations only.
- A test fails if any forbidden field or amount reaches the snapshot.

Tone and design

- Same voice as the X posts: lowercase, dry, a bit self-deprecating.
- Mobile-first, dark by default.
- Footer: "nfa. just a bot trading its own bag." and a link to @decentradork.

### M8b: private admin, admin.dorkbot.dev

Hosting and network

- Served from dorkbot (FastAPI + Next.js), proxied through Cloudflare.
- Server firewall: 443 only from Cloudflare IP ranges; SSH with key only. No Tailscale.

Auth

- Single user. Strong password (argon2 hash) plus TOTP 2FA.
- TOTP is asked again for the kill switch, for resuming after the emergency brake, and for every parameter change.
- Rate limiting and lockout after repeated failures.
- Session cookies: Secure, HttpOnly, SameSite=Strict, short-lived. CSRF protection.
- Email the owner on every login from a new IP, every parameter change and every kill-switch action.

Isolation

- The admin runs in its own container with no Bybit key and no trading secrets in its environment.
- It writes parameter changes and kill-switch requests to the database. The executor applies them only after checking them against hard bounds in code (leverage never above 10, never beyond the AI subaccount limits).
- A test asserts the admin container has no Bybit credentials.

Content

- Everything the public site hides: amounts, balances, positions in size, costs per model, X reads and posts, fees and borrow interest against the monthly budget.
- Full decision log with raw agent inputs and outputs.
- Performance per agent and per model.
- Parameter editor with change history.
- Kill switch, and the "resume" action after the emergency brake (the only action the owner ever has to take).
- Health: scheduler and executor heartbeats, data freshness per source, last backup, days until the Bybit key expires.
- Audit log of every admin action.

## 13. Config defaults (`config.toml`)

```toml
[trading]
assets = ["BTC", "ETH", "SOL", "BNB", "SPX6900"]
capital_max_usdt = 10000        # paper equity; live starts at 10% of it, never beyond actual subaccount equity
live_allowed = false            # owner flips once
leverage_max = 10
leverage_max_spx6900 = 3
risk_per_trade = 0.005
capital_share_per_asset = 0.20
gross_exposure_max = 3.0
holding_min_hours = 1
holding_max_days = 7
short_allowed = true
day_loss_stop = -0.02
drawdown_pause = -0.10
emergency_brake = -0.25
depth_cap = 0.05
cycle_hours = 4

[exchange]
price_feed = "ws"               # public WebSocket with REST fallback; go-live requires "ws"
backup_stop = true              # exchange-side stop behind every real position

[posting]
enabled = true
handle = "decentradork"
language = "en"
style = "nonchalant"
delay_minutes = [1, 10]
max_posts_per_day = 20
post_in_shadow = false

[posting.cashtags]
BTC = "$BTC"
ETH = "$ETH"
SOL = "$SOL"
BNB = "$BNB"
SPX6900 = "$SPX"

[budget]
api_usd_per_month = 550         # above this, downgrade models; never stop
x_reads_per_day = 600

[models]
pm_1 = "claude-fable-5-1"
pm_2 = "gpt-5.6-sol"            # verify exact OpenAI model ID
macro = "gpt-5.6-terra"         # verify
chart_patterns = "claude-opus-5-5"
indicators = "claude-haiku-5-5"
polymarket = "gpt-5.6-luna"     # verify
x_sentiment = "claude-sonnet-5-5"
post_writer = "claude-sonnet-5-5"
post_auditor = "gpt-5.6-terra"  # verify
```

## 14. Milestones

Each ends with passing tests and a README section. Shadow mode starts at M6 and runs at least 6 weeks while M7–M9 are built, so get to M6 fast.

| # | Milestone | Done when |
| --- | --- | --- |
| M0 | Connectivity | Bybit EU client works: server time, balances, `instruments-info` for all five symbols (confirms SPXUSDC and margin/borrow per pair), fee tier. Polymarket and X read smoke tests. `make selftest-live CONFIRM=yes` places and cancels one minimal order. A connectivity report in `docs/M0_REPORT.md`. |
| M1 | Data + DB | Fetchers on schedule, Postgres schema, candles and books stored, data-age tracking. |
| M2 | Indicators agent | Deterministic scores plus a backtest harness on historical candles. |
| M3 | Other four agents | Schemas, prompts, cost tracking, provider swap per agent. |
| M4 | Decision layer | Evidence pack, both PMs, formula anchor, consensus, full logging. |
| M5 | Leverage + risk | All of §8, with unit tests on every formula and limit. |
| M6 | Executor + shadow | Paper simulator, reconciliation, Telegram, kill switch. Shadow mode starts here. |
| M7 | X poster | Writer, auditor, number whitelist, templates, threading, delay. Off in shadow by default. |
| M8 | Public site + admin | M8a public site on dorkbot.dev from a whitelisted snapshot; M8b private admin on admin.dorkbot.dev with 2FA, kill switch, parameters, health. See §12. |
| M9 | Go-live checker | Automatic go-live per §10, leverage ramp, weight auto-tuning. |

## 15. Verify during the build

- Is SPX6900 listed on Bybit EU? If not, trade the other four and keep SPX6900 on the dashboard only.
- Spot-margin borrowing (for shorts) per pair on Bybit EU.
- Fee tier and borrow rates on Bybit EU; feed them into the cost rule.
- Exact OpenAI model IDs for GPT-5.6 Sol, Terra and Luna.
- Sources for ETF flows, stablecoin supply and the economic calendar.
- Subaccount support on Bybit EU. Preferred: a dedicated subaccount holding only the bot's capital.
