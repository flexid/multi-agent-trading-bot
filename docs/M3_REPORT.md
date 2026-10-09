# M3 report: LLM layer and the four model-backed agents

2026-10-09. **Complete.** Lint, mypy and 63 tests pass. All five agents run live from Postgres and return the §6 contract; every model call is logged with cost.

## Done

| Piece | State |
| --- | --- |
| Model IDs | All nine in `config.toml` exist on both providers: `gpt-5.6-sol`, `-terra`, `-luna`; `claude-fable-5-1`, `-opus-5-5`, `-sonnet-5-5`, `-haiku-5-5`. Comments "verify" removed. |
| LLM layer `app/llm/` | One `complete(task, Schema, prompt, user_text, images)` for every call. Model from `config.models[task]`, provider from the model prefix (Anthropic `messages.parse`, OpenAI `responses.parse`), output validated against the pydantic schema, timeout, one retry, then `LLMError`: the caller gets nothing and must not trade. Prompts in `app/prompts/*.md` with a `<!-- version: N -->` header. |
| Cost logging | Every call lands in `llm_calls`: task, asset, model, prompt version, tokens (incl. cached), cost from `[llm.pricing]`, latency, attempts, outcome, parsed output. Live check: Haiku $0.00004, Luna $0.0001 per smoke call. |
| Polymarket agent | Fetch now pages the crypto tag (494 markets: BTC 138, ETH 110, SOL 40, BNB 2, SPX6900 0) plus the Fed-decision tag and the S&P 500 close ladders. Daily mapper (`gpt-5.6-luna`) labels asset, threshold and direction per market; validated and stored, 391 markets for $0.03. Scoring in code: implied median from the ladder (level) and the 24-hour shift (weighs 2:1), confidence from market count and liquidity, so BNB and SPX6900 get confidence 0. |
| Macro agent | Code: z-scores of 5-day changes for 2y/10y yields, dollar, VIX, S&P 500, Nasdaq, gold (Bybit XAUUSDT) and stablecoin supply (DefiLlama); FOMC/CPI/jobs calendar for 2026 (`app/data/calendar_2026.toml`) with the 48-hour window and the "event today" check for SPEC §8; per-asset coupling from 7- and 30-day correlations with equities, dollar (sign flipped) and gold, negative parts floored at 0. Model (`gpt-5.6-terra`): regime, confidence, 48-hour event risk, three reasons. Output = regime × confidence × coupling. Live: neutral at 0.62, couplings 0.20–0.33. |
| X sentiment agent | Reads per 4h cycle: curated account timelines (`[x] accounts`, placeholder until the owner's list) plus a recent search per asset, half the allowance each, capped at `reads_per_cycle_max = 90` and the 600/day budget. `claude-sonnet-5-5` labels asset, stance, kind, credibility and shock per post (schema, stored in `x_posts`; raw text reaches only the labeller). Code aggregates with credibility × kind weights, a neutral prior so two shill posts are not a view, one-sided-crowd halving (contrarian caution), a news-shock flag within 6 hours, and 1.5× count weight for SPX6900. Live: 86 posts labelled for $0.07; search hits are 60% shill at credibility ~0.2, which is why the curated list matters. |
| Chart patterns agent | Code track per 1h/4h/1D: fractal swings clustered into levels, 30-bar range position, volume-confirmed breakouts and false breakouts, regression trendline slope in ATR. Vision track: mplfinance renders (120 bars, EMA 20/50, volume) read by `claude-opus-5-5` into pattern, direction, confidence, key levels. Agreement averages both and raises confidence; a missing read halves the code score; disagreement quarters it. Live: 15 reads for $0.11, 4h down-breakouts agreed on all five assets. |
| Indicators summary | `claude-haiku-5-5` writes two sentences from the indicator table; on any failure the numeric output is used unchanged. |
| Provider swap | `[models.shadow_alt]` names the other provider per task; the cycle runner (M4) runs both in shadow mode and the weight tuning (M9) keeps the better one. |
| Agent runners | `python -m app.agents.run_indicators`, `run_polymarket`, `run_macro`, `run_x [--no-fetch]`, `run_chart [--no-vision] [--save-charts DIR]`; `python -m app.agents.polymarket_map` (daily). |

## Cost per full agent pass (live, 2026-10-09)

| Call | Model | Cost |
| --- | --- | --- |
| Polymarket mapping (daily, ~400 markets) | gpt-5.6-luna | $0.03 |
| Macro regime | gpt-5.6-terra | <$0.01 |
| X labels (~90 posts) | claude-sonnet-5-5 | $0.07 + ~$0.45 of X reads |
| Chart vision (15 images) | claude-opus-5-5 | $0.11 |
| Indicator summaries (5) | claude-haiku-5-5 | <$0.01 |

About $0.70 per 4-hour cycle before the PMs, roughly $130 a month at 6 cycles a day; the PMs (M4) come on top. OpenAI rows use placeholder prices.

## Found along the way

- Gamma's `tag_slug` filter is ignored; `tag_id=21` (crypto) and `tag_id=102000` (macro indicators) work. Pages cap at 100.
- Stooq blocks scripted downloads with a JavaScript challenge; gold comes from Bybit's `XAUUSDT` commodity perpetual instead.
- No free ETF-flow API; deferred (dashboard-only later).
- OpenAI prices for the `gpt-5.6` models are placeholders in `[llm.pricing]` until the owner fills them in; Anthropic prices are exact.

## Needed from the owner

Nothing blocking for M4. At your own pace, in `config.toml`:

1. `[llm.pricing]`: the three `gpt-5.6-*` rows; `[budget] api_usd_per_month` if 550 is not the number.
2. `[x] accounts`: your 50–100 handles, replacing the placeholder.

For M8 later: whether `dorkbot.dev` is on Cloudflare, and the email provider for admin alerts.
