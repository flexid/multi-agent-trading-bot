# M2 report: indicators agent and backtest harness

2026-10-09. Lint, mypy and 35 tests pass.

## What works

| Piece | State |
| --- | --- |
| Agent contract `AgentOutput` (SPEC §6): score, confidence, horizon, evidence, risk flags, data age, `valid` after 30 min | `app/agents/schema.py`; every later agent returns it |
| Indicators agent, pure pandas on closed candles: EMA 20/50/200 with ADX scaling, MACD/ATR, RSI, OBV, realized vol, funding, OI change | `make indicators` runs on all five assets from Postgres; perp funding and OI now fetched every 15 min (`perp_metrics`) |
| Determinism and look-ahead tests: same input gives same score; altering future bars leaves past scores unchanged | In `tests/test_indicators.py` |
| Backtest harness: score at every bar, Spearman IC, hit rate and top-minus-bottom-quintile spread against 4h / 1d / 3d forward returns | `make backtest ARGS="--interval 240 --days 180"` |
| Candle backfill to 1,000 bars per pair and interval | 4h history back to late April; 1D back to 2023 |

Live reading at 03:10 UTC: all five assets score negative (−0.27 to −0.53, confidence 0.74–0.90) with price below EMA 20/50, MACD negative and OBV falling; RSI in the 20s on ETH, SOL, BNB pulls the other way.

## What the backtest says

4h bars, last 180 days, 789 scored bars per asset:

| Asset | IC 4h | IC 1d | IC 3d | Hit 1d | Spread 3d |
| --- | --- | --- | --- | --- | --- |
| BTC | +0.01 | −0.01 | −0.03 | 47% | +1.3% |
| ETH | −0.04 | −0.06 | −0.10 | 44% | +1.5% |
| SOL | +0.02 | +0.03 | −0.01 | 48% | +1.5% |
| BNB | −0.04 | −0.09 | −0.12 | 44% | −0.9% |
| SPX6900 | −0.08 | −0.15 | −0.16 | 42% | −1.3% |

Daily bars over 900 days: IC within ±0.03 everywhere.

**Conclusion: the fixed-rule indicator score has no measurable edge in this window, and a slight tendency to be wrong on 1–3 days for ETH, BNB and SPX6900** (the period was mean-reverting, and the trend components dominate). The rules were not changed in response: tuning them on the window they were tested on would be curve-fitting. The spec already handles this: during shadow mode each agent's weight moves with its measured value and can go to zero (§7, §10). The harness is what re-asks the question every two weeks.

## What doesn't work yet, or is deferred

| Item | Finding |
| --- | --- |
| LLM summary of the indicator table (`claude-haiku-5-5`) | M3, with the LLM layer. Code produces the evidence lines already. |
| Funding and OI history in the backtest | Only stored from today; the backtest uses the candle-only components (weights renormalized). Both accumulate from now on. |
| OI 24h change | Reads 0 until 24 hours of `perp_metrics` exist. |
| Single interval | The agent scores the 4h series; 1h and 1D are stored but unused by it. Multi-timeframe weighting is a decision for after shadow data exists. |

## Needed from the owner

1. OK to commit M2.
2. Still open from M0/M1: the curated X account list (M3); subaccount permissions on `AIsub592625865` (transfers off, cap limit). The old `decentradork` master-account key is deleted. Deployment target: `root@164.90.211.109` (FRA1).
