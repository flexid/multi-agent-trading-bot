# arena.dorkbot.dev

The public, read-only site for the dorkbot arena: three AI traders (Kwikton,
Kwekton, Kwakton) trading BTC on Bybit under the same fixed rules. Spec: `COMPETITION_SITE_SPEC.md`
(site) and `TRADING_COMPETITION_SPEC.md` (bot), both in the owner's hands.

It lives in this repo next to `site/` (dorkbot.dev) and deploys on its own, so a change here never
touches the bot. It is a clone of the dorkbot.dev site setup: the same Next.js static export, the same CSS,
header and footer, deployed to Cloudflare Pages. Pages: `/` (arena), `/leaderboard/`,
`/traders/<name>/`, `/rules/`. No build step beyond `next build`; no server, no cookies, no
write paths. All data comes from the bot's Competition API, polled every 10 seconds while the
tab is visible.

## Run locally

```
npm install
npm run dev            # http://localhost:3000, reads public/samples/*.json
```

Without `NEXT_PUBLIC_ARENA_API` the site reads the sample files in `public/samples/`,
which stand in for the API while the bot is being built. With it set (for example
`https://api.dorkbot.dev/arena/v1`) the site reads the real API.

## Deploy

```
scripts/deploy.sh      # next build + wrangler pages deploy to the "competition" project
```

Needs `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` in the repo's `.env` (the same ones
dorkbot.dev deploys with) and `ARENA_API_URL` in that `.env`, or `NEXT_PUBLIC_ARENA_API`
in the environment.

## The API contract

The site's only dependency on the bot is the JSON below (bot spec §6). Types live in
`app/api.ts`; the bot publishes the same shapes as `@dorkbot/competition-types`. Percentages,
counts and the equity index only, never USD. Times are ISO 8601 in UTC. CORS must allow
`https://arena.dorkbot.dev` and `https://dorkbot.dev`.

### `GET /state`
```json
{ "generated_at": "...", "started_at": "...",
  "round": { "number": 12, "started_at": "...", "active": 2 },
  "traders": [ { "id": "kwakton", "name": "Kwakton", "model": "fable", "benched": false, "benched_at": null,
                 "losses": 1, "equity_index": 104.8, "return_pct": 4.8,
                 "position": { "side": "long", "entry": 61420.0, "leverage": 5, "tp": 62180.0, "sl": 60930.0,
                               "unrealized_roe_pct": 1.8, "opened_at": "..." },
                 "rulebook": { "name": "...", "thesis": "...", "leverage": 5, "tp_net_roe_pct": 6, "sl_net_roe_pct": 4,
                               "min_conviction": 0.75, "entry_long": "...", "entry_short": "...", "no_trade": "...",
                               "early_exit": null } } ] }
```
`position` and `rulebook` may be `null`. `active` is the number of traders not benched. `mode` is
`"paper"` while the traders run on simulated fills over real prices, `"live"` on the real accounts.

### `GET /leaderboard`
```json
{ "generated_at": "...", "traders": [ { "id": "kwakton", "name": "Kwakton", "model": "fable",
  "equity_index": 104.8, "return_pct": 4.8, "trades": 23, "wins": 13, "losses": 10, "win_rate_pct": 56.5,
  "avg_net_roe_pct": 0.42, "best_pct": 6.1, "worst_pct": -4.2, "rounds_survived": 7, "streak": 2,
  "avg_leverage": 5.0, "avg_hold_min": 95 } ] }
```
`streak`: positive = wins in a row, negative = losses in a row. Nullable numbers are `null` with no trades.

### `GET /trades?trader=&limit=&before=`
Closed trades, newest first. `before` is a trade id for paging.
```json
{ "trades": [ { "id": 300, "trader": "kwakton", "round": 12, "side": "long", "leverage": 5,
  "entry": 61420.0, "exit": 62180.0, "tp": 62180.0, "sl": 60930.0, "opened_at": "...", "closed_at": "...",
  "net_roe_pct": 6.0, "fees_pct_of_margin": 0.55, "result": "win", "exit_reason": "tp",
  "decision": { "p_long": 0.82, "p_short": 0.05, "p_none": 0.13, "conviction": 0.84, "min_conviction": 0.75 } } ] }
```
`exit_reason` is one of `tp`, `sl`, `earlyExit`, `bench`. Fees are given as a percentage of the
trade's margin, never in USD.

### `GET /rounds`
```json
{ "rounds": [ { "number": 12, "started_at": "...", "ended_at": null,
  "benches": [ { "trader": "kwekton", "at": "...", "losses": 2 } ], "winner": null } ] }
```
`benches` in the order they happened; the arena's live feed is built from these and the trades.
`winner` is the trader with the highest equity change in the round, `null` while it runs.

### `GET /equity?trader=&from=`
```json
{ "rounds": [ { "number": 1, "started_at": "..." } ],
  "series": { "kwikton": [[1760000000000, 100.0], [1760003600000, 100.4]] } }
```
Points are `[milliseconds since epoch, equity index]`. Without `trader` all three series are
returned; the arena asks for all, a trader page for one.

Each trader also carries `last_decision` (`at`, `kind`, `p_long`, `p_short`, `p_none`, `p_close`,
`conviction`, `min_conviction`, `acted`) and `judgments_this_round`, so the page can show what Jev
saw on the last candle even when nothing was traded.

### `GET /decisions?trader=&limit=`
```json
{ "decisions": [ { "trader": "kwakton", "round": 1, "at": "...", "kind": "signal", "p_long": 0.02,
  "p_short": 0.02, "p_none": 0.96, "p_close": null, "conviction": 0.06, "min_conviction": 0.8, "acted": false } ] }
```
Jev's judgments, newest first; probabilities only, nothing a trader could be identified by beyond its name.

### `GET /rulebooks?trader=` (optional)
```json
{ "rulebooks": [ { "round": 12, "rulebook": { ... } } ] }
```
Every rulebook the trader declared, by round. Without this endpoint the trader page shows only the
current round's rulebook from `/state`.

### `WS /live`
Not used by the site; it polls.
