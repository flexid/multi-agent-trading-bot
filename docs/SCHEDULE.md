# What runs when

Two long-running processes on the droplet, all times UTC. Source of truth:
[`app/scheduler.py`](../app/scheduler.py) (cron jobs) and
[`app/execution/executor.py`](../app/execution/executor.py) (the trading loop).

## Executor: continuous, no cron

| cadence | what |
|---|---|
| every **10 s** (`TICK_S`) | full pass: refresh quotes (WebSocket, REST fallback); apply control requests (kill, pause, resume) and parameter changes from the admin; evaluate every open position's stop, target, trailing stop, time-stop and liquidation guard, send exits; open new positions from decisions younger than one cycle; flush due X posts; equity snapshot per track |
| on every pushed price | fast pass: stops, targets, trailing stops, time-stops and pending exits only (no entries, no accounting) |
| on start | collateral switch for the traded coins; reload open positions and re-arm their stops; backup stop housekeeping |

Exits never wait for a cycle. The exchange-side backup stop (bot stop ± 0.5 ATR) rests on Bybit for every real position and is moved with the trailing stop.

## Scheduler: cron

| when | job | notes |
|---|---|---|
| :00 / :15 / :30 / :45 (+20 s) | data fetches | candles 1h/4h/1d, 200-level books, account, perp funding and OI, margin terms, Polymarket prices (whole crypto events), Fear & Greed + BTC dominance |
| :01 / :16 / :31 / :46, and at startup | Polymarket mapping | parser first, model for the leftovers |
| :03 / :18 / :33 / :48 | trigger check | price move > 2× ATR within an hour, Polymarket shift > 10 pp within an hour, or a credible X news shock → a **triggered cycle**, at most 2 per day |
| :05 every hour | macro | FRED series |
| every 5 min (+40 s) | public site | sanitized snapshot to R2, OG image when it changed |
| every minute (:30 s) | heartbeat | the admin shows staleness after 5 min |
| **00:02, 04:02, 08:02, 12:02, 16:02, 20:02** | **the cycle** (`cycle_hours = 4`) | five agents score each asset (X search per ticker + curated accounts + mention counts, labelling), both PMs propose, consensus, risk engine, decisions stored; the executor acts on them within 10 s. Measured: ~140 s, ~$0.60 per cycle |
| 00:40 daily | go-live check | SPEC §10 criteria, flips `risk_state.mode` when all hold; also classifies wick-outs for stopped trades whose holding window ended |
| 01:10 daily | backup | `pg_dump`, 14 days, private bucket |
| 01:30 on the 1st and 15th | weight tuning | agent weights only after ≥ 100 closed primary trades; the stop buffer moves on the wick-out rate once 20 stops exist |
| 02:00 on the 1st | cost report | LLM, X, server |
| Monday 06:00 | weekly memo | the week's facts in code plus model-written proposals, written to `logs/memo-<date>.md` and emailed to the owner; nothing is applied automatically |

## Changing the pace

- `cycle_hours` in `config.toml` (`[trading]`) sets the schedule; the trigger check, fetches and the executor are independent of it.
- `MAX_TRIGGERED_PER_DAY` and `ATR_MULT` in `app/triggers.py` decide how readily an unscheduled cycle fires; raising the cap and lowering the multiple is the cheap way to react faster, since they only cost a cycle when something moves.
- Cost scales with cycles: 6/day ≈ $110/month, 12/day ≈ $220, 24/day ≈ $440 of the `api_usd_per_month` budget.
