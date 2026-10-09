# M6 report: executor, paper simulator, shadow mode

2026-10-09 04:25 UTC. **Complete; shadow mode is running on `dorkbot`.** Lint, mypy and 106 tests pass, including the owner's seven pre-shadow failure modes.

## What works

| Piece | State |
| --- | --- |
| Executor, own process (`app/execution/executor.py`) | Every 10 s: quotes, control requests, exits (stop, target, trailing stop armed after +1R, time-stop, liquidation, risk close-all, kill), entries for decisions with `action = "open"` while price is within ±0.5% of the planned entry, interest accrual, ledgers, equity snapshot, heartbeat. Decides the mode itself (`paper` until M9 flips it). |
| Order gateway (`gateway.py`) | One seam to the venue: `PaperGateway` (shadow), `ScriptedGateway` (tests), `BybitGateway` arrives with the live path in M9. Outcomes: filled, partial, rejected, borrow failed, unreachable; the executor handles each (partial keeps the filled part; rejected/borrow-failed mark the decision; unreachable retries next tick with stops still armed). |
| Paper simulator (`simulator.py`) | Fills at the touch with the taker fee, hourly borrow interest on the borrowed part, Bybit cross-margin liquidation with collateral ratios, trailing stop, P&L on price and on margin. |
| Two shadow tracks | Ledger 1 `primary` at the live ceiling (2x now), ledger 2 `max` at `leverage_max`; separate positions and equity snapshots (`paper:primary`, `paper:max`). Go-live criteria use the primary. |
| Risk integration | `apply_risk` reads the primary ledger (equity, day P&L, day high, gross exposure, losing-day streak), borrow rate and collateral ratio per coin (fetched every 15 min), book depth with the truncation flag. |
| Controls | `make kill REASON=...`, `make resume`, `python -m app.control pause`; applied by the executor within one tick, logged in `control_requests`. Resume also clears the emergency brake (owner-only action). |
| Scheduler | 15-min fetches (now 9 sources), decision cycle at :02 every 4 h, Polymarket mapper daily 02:30, trigger check every 15 min (2× ATR move, 10 pp Polymarket shift, credible X shock; max 2 triggered cycles per day), heartbeats. |
| Deployment | `make deploy`: Docker on the droplet, repo + `.env` in `/opt/dorkbot`, Compose services postgres / migrate / scheduler / executor with restart policies. First deploy 04:12 UTC, redeploy 04:22 UTC. |
| Tests | Failure modes against a dedicated `bot_test` database (`TEST_DATABASE_URL`): partial fill, rejected order, feed drop with an active stop, restart mid-trade, failed borrow, gap through a stop (fills at market, not at the stop), exchange unreachable (exit retried until it lands, entry retried without duplication). |

## Owner briefing (six rules) — all implemented

See `docs/DECISIONS.md`, "M6 owner briefing". Summary: no trade when one PM is flat; `capital_max_usdt = 10000`, live at 10% and never beyond subaccount equity; primary track on live rules plus a `leverage_max` comparison track; Bybit collateral-ratio liquidation; 200-level books with a truncation flag; the seven tests above.

## Not yet, or deferred

| Item | Finding |
| --- | --- |
| Server cannot read the account | `bybit.account` fails with `10010 Unmatched IP`: the key is bound to the owner's IP. Add `164.90.211.109` to the key's whitelist. Paper trading is unaffected. |
| Live order gateway | `BybitGateway` (post-only limit near mid, reprice after 2 min, fills over the private WebSocket) comes with M9, when it can be exercised in the live self-test. |
| Reconciliation against the exchange | Runs in live mode only (compares positions and borrows with the account snapshot); in shadow the ledger is internal. |
| Price feed | REST ticker every 10 s with stale detection; the public WebSocket replaces it before go-live. |
| Maintenance margin rate | `[risk] maintenance_margin_rate = 0.03`, not exposed by the API; confirmed in the live self-test (M9). |
| Open-position re-assessment by the PMs (hold / adjust / close) | The pack carries the slot; the cycle fills it in M7 alongside the X poster's close posts. |

## Needed from the owner

1. Add `164.90.211.109` to the Bybit API key's IP whitelist.
2. Scheduled for tomorrow: `[llm.pricing]` / `[budget]`; CoinGecko demo key; Cloudflare status of `dorkbot.dev` (M8a); email provider for admin alerts (M8b).
