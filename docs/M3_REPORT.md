# M3 report: LLM layer and the four model-backed agents

Status 2026-10-09 03:45 UTC: **in progress.** Lint, mypy and 50 tests pass. Updated as each agent lands; the final version is committed with the milestone.

## Done

| Piece | State |
| --- | --- |
| Model IDs | All nine in `config.toml` exist on both providers: `gpt-5.6-sol`, `-terra`, `-luna`; `claude-fable-5-1`, `-opus-5-5`, `-sonnet-5-5`, `-haiku-5-5`. Comments "verify" removed. |
| LLM layer `app/llm/` | One `complete(task, Schema, prompt, user_text, images)` for every call. Model from `config.models[task]`, provider from the model prefix (Anthropic `messages.parse`, OpenAI `responses.parse`), output validated against the pydantic schema, timeout, one retry, then `LLMError`: the caller gets nothing and must not trade. Prompts in `app/prompts/*.md` with a `<!-- version: N -->` header. |
| Cost logging | Every call lands in `llm_calls`: task, asset, model, prompt version, tokens (incl. cached), cost from `[llm.pricing]`, latency, attempts, outcome, parsed output. Live check: Haiku $0.00004, Luna $0.0001 per smoke call. |
| Polymarket agent | Fetch now pages the crypto tag (494 markets: BTC 138, ETH 110, SOL 40, BNB 2, SPX6900 0) plus the Fed-decision tag and the S&P 500 close ladders. Daily mapper (`gpt-5.6-luna`) labels asset, threshold and direction per market; validated and stored, 391 markets for $0.03. Scoring in code: implied median from the ladder (level) and the 24-hour shift (weighs 2:1), confidence from market count and liquidity, so BNB and SPX6900 get confidence 0. |
| Macro agent | Code: z-scores of 5-day changes for 2y/10y yields, dollar, VIX, S&P 500, Nasdaq, gold (Bybit XAUUSDT) and stablecoin supply (DefiLlama); FOMC/CPI/jobs calendar for 2026 (`app/data/calendar_2026.toml`) with the 48-hour window and the "event today" check for SPEC §8; per-asset coupling from 7- and 30-day correlations with equities, dollar (sign flipped) and gold, negative parts floored at 0. Model (`gpt-5.6-terra`): regime, confidence, 48-hour event risk, three reasons. Output = regime × confidence × coupling. Live: neutral at 0.62, couplings 0.20–0.33. |
| Agent runners | `python -m app.agents.run_indicators`, `run_polymarket`, `run_macro`; `python -m app.agents.polymarket_map` (daily). |

## In progress

| Piece | Plan |
| --- | --- |
| X sentiment agent | `x_posts` table added. Reads per 4h cycle: curated accounts (`[x] accounts` in `config.toml`, placeholder until the owner's list) plus a recent search per asset, capped at `reads_per_cycle_max = 90` (6 cycles ≈ 540 of the 600/day budget). `claude-sonnet-5-5` labels each post (asset, stance, type, credibility, shock); code aggregates into a sentiment level and a news-shock flag, with extreme one-sidedness counted as contrarian caution and extra weight for SPX6900. |
| Chart patterns agent | Code track: levels, trendlines, breakouts, ranges on 1h/4h/1D. Vision track: mplfinance renders in a fixed style read by `claude-opus-5-5`. Full weight only when both agree. |
| Indicators summary | `claude-haiku-5-5` turns the indicator table into two sentences for the evidence pack; the numbers never change. |
| Provider swap | During shadow mode each agent also runs on the other provider (`model=` override exists; the shadow-alt config and the scoring comparison come with the cycle runner in M4). |

## Found along the way

- Gamma's `tag_slug` filter is ignored; `tag_id=21` (crypto) and `tag_id=102000` (macro indicators) work. Pages cap at 100.
- Stooq blocks scripted downloads with a JavaScript challenge; gold comes from Bybit's `XAUUSDT` commodity perpetual instead.
- No free ETF-flow API; deferred (dashboard-only later).
- OpenAI prices for the `gpt-5.6` models are placeholders in `[llm.pricing]` until the owner fills them in; Anthropic prices are exact.

## Needed from the owner

Nothing blocking. At your own pace, both in `config.toml`:

1. `[llm.pricing]`: the three `gpt-5.6-*` rows; `[budget] api_usd_per_month` if 550 is not the number.
2. `[x] accounts`: your 50–100 handles, replacing the placeholder.

For M8 later: whether `dorkbot.dev` is on Cloudflare, and the email provider for admin alerts.
