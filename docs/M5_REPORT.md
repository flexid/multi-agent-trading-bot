# M5 report: leverage agent and risk engine

2026-10-09. **Complete.** Lint, mypy and 90 tests pass; every formula and limit in SPEC §8 has a unit test (`tests/test_risk.py`).

## What works

| Piece | State |
| --- | --- |
| Base leverage (`app/risk/leverage.py`) | `L = min(leverage_max, r / (a × d))`: a 1% stop gives 2.5x, 0.5% gives 5x, as in the spec. |
| Caps, lowest wins | Absolute 10x; live ceiling (2x at start, ramps per §8); SPX6900 3x; ATR above its 30-day 90th percentile → half; FOMC/CPI/jobs today → 2x; risk-off while coupling > 0.5 → 2x; Fear & Greed extreme → 2x (owner briefing); PMs disagree or conviction < 0.5 → 1x and no borrowing; two losing days → half; after a drawdown pause → half risk. An LLM cap can only lower (`llm_cap`), never raise. |
| Liquidation buffer | Cross-margin liquidation distance ≈ 1/L − 5% must be ≥ 3× the stop distance; leverage is reduced in 0.5 steps until it holds. |
| Risk engine (`app/risk/engine.py`) | Account rules in severity order: emergency brake (−25% vs starting capital, or 2 pauses in 30 days, or already engaged), drawdown pause (−10% from peak: close all, 72 h, half risk, 2x cap), paused, day loss stop (−2%: close all until 00:00 UTC), day profit lock (from +1.5%: stops protect +0.75%). Per trade: consensus required, ≥3 valid agents, ≤3 trades per asset per day, shorts need margin and borrowing, cost rule (target ≥ 3× fees + borrow interest for the hold), sizing = capital share × leverage capped by 5% of ±2% book depth and by 3× equity gross exposure. |
| Persistence (`app/risk/apply.py`) | `risk_state` (peak, pauses, brake, ceiling), `risk_rule_hits` per cycle, and `decisions.risk_rule_hits` / `decisions.action` / `decisions.proposal.plan`. Runs at the end of every cycle. |

Cycle 1 re-assessed: SOL and BNB blocked (`short_needs_borrow`: conviction 0.40 < 0.5); BTC, ETH and SPX6900 are agreed shorts that would be sized, blocked by `no_room` because `capital_max_usdc = 0`.

## Not yet, or deferred

| Item | Finding |
| --- | --- |
| Day P&L, gross exposure, losing-day streak | Placeholders (0) until the paper simulator and positions exist (M6). The engine already consumes them. |
| Borrow rate and margin availability per pair | Constants for now; M6 refreshes them from the exchange every cycle. |
| LLM risk flags lowering leverage | The `llm_cap` input exists; the model call that fills it (an agent may flag thin books or news) comes with the executor's pre-trade check in M6. |
| Live leverage ramp (2x → 5x → 10x) and automatic drop-back | `risk_state.leverage_ceiling` holds it; the go-live checker (M9) moves it. Shadow mode uses `leverage_max`. |

## Decisions

See `docs/DECISIONS.md`: "M5: shorts at 1x still borrow" and "M5: Fear & Greed extreme caps leverage at 2x".

## Needed from the owner

1. `capital_max_usdc` in `config.toml` (now 0): shadow sizing uses it as paper equity.
2. Unchanged: `[x] accounts`; `[llm.pricing]` / `[budget]`; optional CoinGecko key; for M8 later, Cloudflare status and the email provider.
