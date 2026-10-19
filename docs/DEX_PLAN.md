# dex.dorkbot.dev: dorkbot trading on a DEX, for people who connect a wallet

A plan, 2026-10-19, on the owner's question whether dorkbot's decisions can trade on a DEX,
for users who connect their own wallet, allocate USDC and let dorkbot trade it, and whether a
coin of our own fits in. The venue is open ("any reliable DEX"); the plan explains why one
stands out. Nothing here is built.

## 1. The idea in one paragraph

dorkbot already produces one stream of approved plans: the decisions the risk engine signs off
on for the main book (the "closed API": `decisions` with `action = open`, the executor's exits,
all in Postgres). A DEX executor mirrors that stream on a decentralised perpetuals exchange,
same entries, stops, takes and closes, for money that stays in the users' own hands: a user
connects a wallet, allocates USDC, and dorkbot trades that allocation without ever being able
to withdraw it. dex.dorkbot.dev is where they connect and watch. Nothing in the decision logic
changes; the DEX side is a follower of the record the site already publishes.

## 0. The one decision that matters: how users' money is held

Trading for other people is a different product from trading our own bag, whatever the venue.
Two non-custodial models exist on DEXs; everything else is custody, which we should not do.

| | **A vault** | **Per-user delegation** |
|---|---|---|
| how it works | users deposit USDC into one pooled vault; dorkbot trades the pool; results are shared pro rata; the protocol handles deposits, withdrawals and the leader's fee | each user approves an "agent key" on their own account that may trade but not withdraw; dorkbot's server holds those keys and trades every account in parallel |
| custody | the protocol's smart contracts, not us | the user's own account, not us |
| what we run | one trading account | one trading loop per user: N accounts, N fills, N sizes, rate limits |
| fairness | identical for everyone | fills differ per user; the last user in the loop gets the worst price |
| user experience | "deposit, watch, withdraw" | "connect wallet, approve dorkbot, allocate" |
| fee | a performance fee the protocol enforces | whatever we build, with our own billing |
| on which DEX | Hyperliquid has it natively; most others do not | Hyperliquid (agent wallets), dYdX v4 (authenticators), Drift (delegates) |

The vault is the stronger product: one account to run, identical outcomes for everyone,
custody by the protocol, withdrawals the user controls, a fee we never touch. Per-user
delegation is "connect your wallet" in the purest sense but multiplies every operational risk
by the number of users, and the fairness problem is real at any size. My recommendation is the
vault first; per-user delegation only if the vault's terms (lock-ups, fee caps) turn out to
block what you want.

Either way this is asset management for third parties. Non-custodial does not mean
unregulated: where you and the users sit decides what a performance fee is, and a lawyer
should see the terms before the first stranger deposits. That is yours, and it is the longest
lead item in this plan.

## 2. Which DEX

"Reliable" for a bot means: deep books on the assets, exchange-held stop and take orders,
an API that does not drop, and the custody model above. Measured that way:

| DEX | depth | TP/SL on the exchange | vaults or delegation | note |
|---|---|---|---|---|
| Hyperliquid | by far the deepest perps DEX | yes | both, native | the only one that fits the product without extra contracts |
| dYdX v4 | good on majors | yes | delegation via authenticators; no vaults for third parties | own chain, Cosmos tooling |
| GMX, Vertex, Aevo | thinner | partly | no native delegation; a contract of our own would be needed | more work, less liquidity |
| Drift (Solana) | decent on majors | yes | delegates; vaults via third-party programs | Solana stack, different keys |

So Hyperliquid is not a must, but it is the one where the whole product exists today. The
adapter is written behind an interface (like the arena's `Exchange`), so a second venue is a
second adapter, not a rewrite.

Is it possible? Yes, and the fit is good:

- **Hyperliquid is built for this.** A public REST and WebSocket API, perpetuals on BTC, ETH,
  SOL, XRP, DOGE and a long tail of alts, up to 50x leverage, take-profit and stop-loss trigger
  orders held by the exchange, sub-second fills, an official Python SDK, and a testnet. Taker
  fees around 0.035%, lower than Bybit's spot fees.
- **Keys stay narrow.** Hyperliquid has "API wallets" (agent keys): a separate key that may
  trade for an account but cannot withdraw. For the vault the server holds one such key; for
  per-user delegation it holds one per user. Nobody's funding key ever reaches the server.
- **The record becomes verifiable.** Every fill is on-chain and tied to the address. The site
  can link each trade to the explorer. That is a stronger claim than any paper book.
- **Everything upstream is reused.** Agents, managers, risk engine, day goal, trailing take,
  posting: unchanged. The only new code is an exchange adapter and a mirror loop, the same
  shape as the arena's `LinearExchange` and `PaperExchange`.

What does not carry over one to one:

- **Perps, not spot margin.** Funding payments every hour (paid or received), liquidation at a
  price the exchange sets per leverage, no borrowing. Sizing and the hard stop need their own
  arithmetic; the arena's rules module already does most of it.
- **The alt list may differ.** Which of PEPE, HBAR, PUMP, SPX6900 and ENA are listed, and at
  what depth, is a check for day one. An unlisted asset is simply skipped.
- **Latency between the two books.** The mirror follows the main book's decisions by seconds.
  Entries at a slightly different price are normal; the mirror keeps its own stop and take at
  the plan's distances from its own fill.
- **Jurisdiction.** Hyperliquid's front end blocks some regions; API access is separate but
  the terms are yours to read. Perps on a DEX are also a tax question. Both are yours.

## 3. Design

```
Postgres (decisions, positions of the main book)
        │  poll every 5 s, or the executor's own control table
        ▼
 app/dex/mirror.py      follows the storyline track: open when it opens, close when it closes
        │
        ▼
 app/dex/hyperliquid.py adapter: market entry with TP/SL triggers, close, position, fills,
                         account value, funding paid; testnet flag; API-wallet key
        │
        ▼
 dex_* tables           every order, fill, funding payment, equity point (USD, never shown)
        │
        ▼
 snapshot for dex.dorkbot.dev: the address, equity index, open trades, closed trades with
 explorer links, funding paid, divergence from the main book per trade
```

- **Follow the record, not the plan twice.** The mirror does not re-run the risk engine. It
  reads what the main book did and does the same, scaled to its own equity. That keeps one
  source of truth and makes the divergence between the two books a measurable number.
- **Its own exits as a safety net.** The TP and SL triggers sit on Hyperliquid at the same
  distances as the main book's soft stop and target. If the mirror loses contact with the
  database, the exchange still closes the trade. The main book's early closes (day goal,
  trailing take, time stop, kill) are mirrored when they happen.
- **Sizing:** the main book's margin share per asset (14% bluechip, 6% alt) applied to the
  wallet's equity, at the plan's leverage, capped by the isolated-margin liquidation distance
  so the hard stop is always hit before liquidation.
- **Reconciliation every cycle,** as the non-negotiables require: positions and balance from
  the exchange against the ledger; on a mismatch, alert and open nothing new.
- **Tests never touch the live endpoint.** A paper twin like the arena's, plus the testnet.

## 4. Milestones

| | what | needs from you |
|---|---|---|
| M1 | adapter on testnet: fills, TP/SL triggers, close, funding, reconciliation; the alt list checked | a Hyperliquid account, a testnet wallet |
| M2 | the mirror loop on paper over the real main book; `dex_*` tables; dex.dorkbot.dev showing the mirror with a paper badge | the subdomain (dex.dorkbot.dev, like the others; dorkbot.com is a different domain) |
| M3 | live on our own small wallet with an API key; alerts; posting off; the divergence from the main book measured for a month | funding, the allocation you can lose, the jurisdiction check |
| M4 | the vault: opened under the record from M3; the site becomes "connect, deposit, watch, withdraw" with the vault's own numbers | the terms, the fee, legal advice, a record worth depositing into |
| M5 | optional: per-user delegation for users who will not use the vault | only if M4's terms block something |

Effort: M1 and M2 about two days each of my time; M3 a day plus a month of watching; M4 a few
days once the record and the terms exist. The month in M3 is not padding: nobody should be
able to deposit into a strategy whose live DEX record is shorter than that.

## 5. A coin of our own

Possible, but the question to ask first is what it would be for. Three honest routes:

1. **The vault is the product; no coin needed.** "Connect, allocate USDC, let dorkbot trade"
   is exactly what a vault does, with the protocol handling custody, shares and withdrawals.
   A coin adds nothing to that mechanism.
2. **A token as access.** A token that gates the closed API (the plans before they are
   public) or the admin's readings. The safest kind of token legally, and the demand is real
   only if the signals are worth paying for, which the record has to show first.
3. **A token as a share of profits.** Buybacks or fee sharing from the bot's results. This is
   a security in most places, the owner would be the issuer, and the bot's record so far
   (-6% on the main book this week, the arena a show) is not something to sell. Not now, and
   not without a lawyer.

My recommendation: build the DEX mirror, run it in public on a small wallet, and let the
record decide. If it earns, the vault is the product and it needs no coin. A coin comes up
again only if there is a reason beyond raising money, and then route 2, with advice.

## 5b. The service fee

Both models carry a fee the protocol enforces, so no billing of our own is needed:

- **Vault:** the leader's share of depositors' profits, calculated and paid by the protocol on
  withdrawal; the leader keeps a minimum stake. Earned only when depositors earn.
- **Per-user delegation:** a builder fee per fill, approved once by the user with a cap when
  they connect, paid to the builder address; a performance fee can sit beside it.

Outside Hyperliquid the fee is ours to build: a pool contract that enforces the share, or a
subscription for access, which is weaker. The plan's choice is the vault with the profit share
as the only fee.

## 6. Open questions for the owner

- Which assets: the ten of today, bluechips only, or whatever Hyperliquid lists of them?
- Follow the main book only, or also offer the max track's leveraged variant as a second wallet?
- Posting: should the DEX trades go to X at all? One story on X is the current rule.
- dex.dorkbot.dev or a different name, and does it get the brand's header or its own?
