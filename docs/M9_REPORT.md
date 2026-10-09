# M9 report: go-live checker, leverage ramp, weight tuning, live gateway, pilot

2026-10-09. **Built and deployed; the live self-test waits for funding.** Lint, mypy and 136 tests pass.

## What works

| Piece | State |
| --- | --- |
| Go-live checker (`app/risk/golive.py`) | Ten criteria on the primary paper track: ≥ 28 shadow days AND ≥ 100 closed trades (owner rule), net positive after fees and interest, profit factor ≥ 1.3, Sharpe above BTC buy-and-hold (daily returns, same window), max drawdown < 10%, zero simulated liquidations, no operational incidents in the last 2 weeks, live self-test passed, `live_allowed = true`. Runs nightly at 00:40 UTC; `make golive` prints the verdict. `decide` flips `risk_state.mode` to live by itself when all hold; the executor then starts at 2x and 10% of `capital_max_usdt`, never beyond subaccount equity. |
| Leverage ramp | 2x at live start; 5x after 4 weeks without a pause and a non-negative month; 10x after 13 weeks net-positive overall; drops back on a drawdown pause or a losing month. Shadow's primary track mirrors the same ceiling. |
| Scoring and tuning (`app/decision/tuning.py`) | IC of each agent's score and each PM model's signed conviction against 4 h / 1 d / 3 d forward returns over all stored cycles; main and alt PM variants compared side by side. Tuning on the 1st and 15th, only after 100 closed primary trades: ±5 pp toward the ranking, zero for agents with IC ≤ 0, shrink halfway to equal, renormalize; stored in `agent_weights`, read by the formula anchor every cycle. Leaderboard feeds the public site. |
| Live gateway (`app/execution/bybit_gateway.py`) | Post-only limit at the touch with our `orderLinkId`, one reprice after 2 minutes, fills read from order history, borrow via `isLeverage=1` on shorts, repay on buy-back; outcomes filled / partial / rejected / borrow failed / unreachable, same contract as the paper gateway. |
| Modes (`executor.decide_mode`) | `live` only when `risk_state.mode = live` AND `live_allowed`; `pilot` when `[pilot] enabled = true` (200 USDT, 1x, own ledger, real gateway, tracked as `positions.mode = "pilot"`, never counted toward go-live); `paper` otherwise. |
| Live self-test (`make selftest-live-full CONFIRM=yes`, server only) | Minimal long and sell-back, minimal short with borrow and cover (asserts no borrow remains), kill switch applied by the executor, then resume; sets `risk_state.live_selftest_at`. |
| Ops (`app/ops.py`) | Nightly `pg_dump` to `logs/backups` (14 kept, pushed to R2 when configured); monthly cost report (`make costs`, `logs/costs.md`): LLM per model, X reads and posts, server; the owner's 4-week cost report comes from it. |

## Needed from the owner

1. 200 USDT in `AIsub592625865`, then "run the live self-test".
2. After it passes: `[pilot] enabled = true` to start the 200 USDT 1x pilot.
3. Still open from M7/M8: style-profile depth; Cloudflare credentials, R2 token, bucket + domain, `SNAPSHOT_PUBLIC_URL`; email provider credential; `[llm.pricing]` / `[budget]`; CoinGecko key; admin setup over SSH.
