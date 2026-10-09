# Decision log

Newest first. One entry per decision: what was decided, why, and what it rules out.

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
