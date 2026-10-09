# M4 report: decision layer

2026-10-09. **Complete.** Lint, mypy and 71 tests pass. First full cycle ran live: 135 s, $0.59 including the alternate-provider PMs.

## What works

| Piece | State |
| --- | --- |
| Evidence pack (`app/decision/evidence.py`) | Per asset: spot, 4h ATR, the five agent outputs (score, confidence, horizon, validity, evidence lines, risk flags), macro regime and coupling, open position (filled from M6), last 10 decisions with the price change since. Built from validated outputs and own records only; no raw text. |
| Two PMs (`app/decision/pm.py`, `app/prompts/pm.md` v1) | `claude-fable-5-1` and `gpt-5.6-sol`, same pack, no tools, in parallel, neither sees the other. Schema: direction, entry zone, stop, target, hold time (1–168 h), score, conviction, three reasons, agents weighted up/down. Geometry is validated (stop and target on the right side, entry zone ordered); a directional proposal for an asset with fewer than 3 valid agents is forced flat; unknown assets and agent names are dropped. |
| Formula anchor (`consensus.py`) | indicators 0.25, chart 0.20, Polymarket 0.20, macro 0.20 × coupling, X 0.15, renormalized over valid agents; `None` below 3 valid agents. |
| Consensus | Same direction: conviction-weighted mean of the two scores, clamped to formula ± 0.4, geometry merged by conviction, conviction = the lower one. Opposite: no trade. One PM flat: the trade proceeds marked `partial` with conviction capped below 0.5, which SPEC §8 turns into 1x without borrowing. Any PM failure or stale data: no trade. |
| Cycle runner (`cycle.py`) | `python -m app.decision.cycle`: all five agents in parallel (a failing agent is simply absent), outputs stored, pack built, both PMs plus the `[models.shadow_alt]` pair, consensus, decisions stored, cycle cost summed from `llm_calls`. Flags `--no-x-fetch`, `--no-vision`, `--no-alt`, `--trigger`. |
| Tables | `cycles`, `agent_outputs`, `pm_proposals` (main and alt variants), `decisions` (with `risk_rule_hits` and `action` for M5/M6). |

First live cycle, 2026-10-09 03:50 UTC: both PMs short on all five assets; consensus −0.27 to −0.55, formula −0.16 to −0.26, conviction 0.40–0.55; stops 1–3 ATR, targets ≥ 2× stop, hold ≤ 62 h.

## Addendum: crypto-native macro inputs (owner briefing, same day)

Fear & Greed (alternative.me, 3,169 days of history) and BTC dominance (CoinGecko `/global` every 15 minutes, ETHBTC/SOLBTC candles as the backtestable proxy) are fetched by the `crypto_native` job. The macro output is split into `tradfi` (regime × confidence × coupling) and `native` (Fear & Greed contrarian at extremes, dominance tilt per asset), both logged in `agent_outputs.components` and shown on the public agents page (SPEC §12). Backtest with `python -m app.backtest_native`: Fear & Greed is a weak caution signal (weight 0.25 plus a risk flag), the dominance proxy has no edge (weight 0, logged only, re-test after 60 days of real dominance). Details in `docs/DECISIONS.md`.

## Not yet, or deferred

| Item | Finding |
| --- | --- |
| Triggered cycles (2× ATR move, 10 pp Polymarket shift, X news shock), max 2/day | The runner accepts `--trigger`; the watchers that fire it come with the scheduler integration in M6. |
| Open-position re-assessment (hold / adjust stop / close) | Needs positions; M6. The pack already carries the slot. |
| Weight auto-tuning | M9, after 100 closed trades. |
| Day P&L in the pack | 0 until the paper simulator (M6) produces it. |
| Alt-provider agents | Only the PMs run on both providers so far; the alt table covers all tasks and the agent runners accept `model=`, so the rest is a scheduler switch. |

## Cost

One cycle: agents ~$0.25 (without X reads), PMs ~$0.17, alt PMs ~$0.17. With X reads (~$0.45) about $1.05 per cycle, ~$190 a month at six cycles a day plus triggered ones. Within the $550 budget; the OpenAI rows are still placeholder prices.

## Needed from the owner

1. `capital_max_usdt` in `config.toml` (now 0): the amount the bot will trade with. M5 sizes paper positions from it.
2. Unchanged, at your pace: `[x] accounts`; `[llm.pricing]` and `[budget]`; for M8 later, Cloudflare status of `dorkbot.dev` and the email provider for admin alerts.
