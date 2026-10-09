# Polymarket funnel: diagnosis and fix (2026-10-09)

Owner briefing: diagnose why the Polymarket agent reports no coverage, replace the model
mapper with a deterministic parser, map incrementally, keep ladders whole, use the Up/Down
markets, and tell "no markets" from "none usable" on the health page.

## Before (server, 2026-10-09 15:30 UTC, ~11 hours after the first fetch)

| stage | count |
|---|---|
| markets fetched and stored | 994 (all `active`, none `closed`) |
| mapped to an asset | **0** |
| usable ladder points, any asset | **0** |
| agent output, every asset | score 0, confidence 0, `no polymarket coverage` |

Causes, in order of damage:

1. **The mapper never ran.** It was a daily cron at 02:30 UTC; the stack came up at
   ~04:10 UTC, so the first mapping would have been the next night. Until then every
   market sat unmapped and the agent could not tell.
2. **The model was on the critical path** for every market, 40 per call, and a failed
   call left the batch unmapped for a day.
3. **The fetch was by market volume.** The busiest crypto markets by 24h volume are the
   5- and 15-minute Up/Down markets (163 of the 994 stored; 119 by slug were 5m/15m),
   which SPEC §6 says to ignore. They crowded the pages and bloated the price table
   (46,585 price rows in 11 hours) while contributing nothing.
4. **The evidence said "no mapped markets" for every failure.** "Nothing on Polymarket"
   and "markets exist but all fail a filter" looked the same.

## What changed

- `app/agents/polymarket_parse.py`: a regex parser for the templated questions
  ("Will the price of Bitcoin be above $84,000 on October 9?", "Will Solana dip to $100
  October 5-11?", "Will S&P 500 (SPX) close at >$7,000 in December?", "Bitcoin Up or Down
  - October 9, 8:00AM-12:00PM ET"). It yields `price` (threshold + direction), `range`,
  `updown` (window in minutes) or `other`. Questions that mention one of our assets in an
  unknown form go to the model; questions that mention none are `other` without a call.
- `app/agents/polymarket_map.py`: parser first, model for the leftovers. Scheduled every
  15 minutes at :01/:16/:31/:46 (right after the fetch) and once at scheduler startup.
- `app/data/fetch.py`: fetches the 100 busiest crypto **events** with all their markets
  (a ladder is one event), then two pages of crypto markets, macro and the S&P search.
  5- and 15-minute Up/Down markets are dropped before storage. Markets past their end
  date that Gamma no longer returns are marked closed.
- `app/agents/polymarket.py`: Up/Down markets (hourly, 4-hour, daily) add a momentum
  component: volume-weighted P(up), centred on 50%, 65% = +1. Each window weighs by the
  fraction still ahead; the last quarter of a window is history and does not count.
  Weights: 24h shift 0.5, level 0.25, up/down 0.25, renormalized over what exists.
  `coverage()` counts every drop reason per asset.
- Admin overview: a "Polymarket coverage" table (open markets, unmapped, ladder points,
  up/down, status) with "no markets" in red, "N markets, none usable (reasons)" in amber.
- The agent's evidence and risk flag now distinguish `no polymarket coverage` from
  `polymarket: none usable`.

## After (server, 2026-10-09 15:45 UTC, first mapping pass)

Mapping: 1,013 open markets, 1,010 by the parser, 20 by the model (3 `other`, 5 BTC
non-price, 1 ETH non-price, 11 S&P 500 close markets in a phrasing the parser does not
cover). Model calls dropped from "every market, daily" to "about 20 questions, once".

| asset | open markets | ladder points | up/down | dropped (reason: n) | agent |
|---|---|---|---|---|---|
| BTC | 153 | 75 | 3 | >60 d: 25 · range: 18 · <1 h: 13 · thin: 14 · non-price: 5 | conf 0.50 |
| ETH | 128 | 58 | 3 | >60 d: 20 · range: 15 · <1 h: 11 · thin: 18 · non-price: 3 | conf 0.50 |
| SOL | 54 | 18 | 1 | <1 h: 7 · >60 d: 8 · thin: 15 · range: 5 | conf 0.50 |
| BNB | 2 | 0 | 0 | >60 d: 2 | `none usable (2 resolve beyond 60 days)` |
| SPX6900 | 0 | 0 | 0 | – | `no markets` |

Confidence sits at 0.50 because the 24h shift needs 20+ hours of stored prices; from
2026-10-10 morning the shift component is live and confidence can reach 1.0.

BNB and SPX6900 are as SPEC §6 expects: Polymarket has almost nothing on them, so the
agent weighs little there. The health page now says so in words instead of a zero.
