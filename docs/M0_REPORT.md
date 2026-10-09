# M0 connectivity report

Run: 2026-10-09 02:05 UTC, `make selftest` against `https://api.bybit.com`, from a development machine (not the server). 33 checks, 0 failed, 6 warnings. Raw results: `logs/selftest.json` (gitignored).

## Summary

- The Bybit client, Polymarket and X reads all work. Lint, type checks and 23 tests pass.
- The API key is a **Bybit global** key. `api.bybit.eu` rejects it (`10003 API key is invalid`), `api.bybit.com` accepts it. The owner switched the host to `api.bybit.com` during M0; see `docs/DECISIONS.md`.
- **SPX6900 is listed** as `SPXUSDC`, but that pair has no margin and almost no liquidity. `SPXUSDT` has both. The same holds for BNB.
- **The account cannot borrow yet:** it is in isolated margin mode with spot margin switched off.
- **`make selftest-live` has not been run.** The account holds 0 USDC, and a real order needs the owner's go-ahead.

## What works

| Check | Result |
| --- | --- |
| Server time | OK, local clock 71 ms off; the client corrects for it |
| `instruments-info`, tickers, order books | OK for all five assets, in USDC and USDT |
| Signed reads: key info, account mode, balances, fee rates, open orders | OK |
| Public borrow terms per coin | OK |
| Perp funding and open interest (indicator input) | OK |
| Paper round-trip (buy at ask, sell at bid, taker fee, exchange tick and lot rounding) | OK |
| Polymarket Gamma, CLOB (book, midpoint, 24h history), Data API | OK; the JSON-string fields parse |
| X: user lookup for @decentradork, recent search | OK; 10 posts read, about $0.06 per self-test run |

## SPX6900 listing

Listed and trading as `SPXUSDC`. The price matches CoinGecko's `spx6900` (0.368 against 0.369), so it is the memecoin.

It is not usable as specified: margin is off for the pair, 24-hour turnover is $18.6k, the spread is 84 bps, and only $314 / $438 rests within ±2% of mid. The 5% depth cap would limit a position to about $16.

`SPXUSDT` trades $2.3M a day at an 8 bps spread, with margin and shorting. Its depth allows roughly $1,770 per position.

## Margin and borrowing per pair

"Margin" is the pair's `marginTrading` flag. A short also needs the base coin to be borrowable; a leveraged long needs the quote coin to be. Borrow rates are for the account's VIP level ("No VIP"), annualized from the hourly rate.

| Pair | Margin | Short | 24h turnover | Spread | ±2% depth (bids / asks) | Borrow APR, base coin |
| --- | --- | --- | --- | --- | --- | --- |
| BTCUSDC | yes | yes | $46.5M | 0.0 bps | $966k / $680k | 0.4% |
| ETHUSDC | yes | yes | $16.2M | 0.0 bps | $507k / $623k | 1.9% |
| SOLUSDC | yes | yes | $3.7M | 0.9 bps | $137k / $88k | 4.5% |
| BNBUSDC | **no** | **no** | $0.12M | 5.4 bps | $6.8k / $9.0k | – |
| SPXUSDC | **no** | **no** | $0.02M | 83.6 bps | $314 / $438 | – |
| BTCUSDT | yes | yes | $1,143M | 0.0 bps | $3.35M / $1.72M | 0.4% |
| ETHUSDT | yes | yes | $317M | 0.0 bps | $2.00M / $1.67M | 1.9% |
| SOLUSDT | yes | yes | $75.2M | 0.9 bps | $1.87M / $1.78M | 4.5% |
| BNBUSDT | yes | yes | $7.5M | 1.4 bps | $362k / $300k | 4.4% |
| SPXUSDT | yes | yes | $2.3M | 8.1 bps | $46k / $35k | 5.5% |

Borrowing the quote coin (for leveraged longs) costs 4.7% a year for USDC and 3.9% for USDT.

With USDC as quote, BNB and SPX6900 are 1x long-only and capped at about $340 and $16 per position. With USDT, all five assets can be leveraged and shorted.

SPX has a collateral ratio of 0.6 (BTC 0.98, ETH 0.95, SOL and BNB 0.9), which the liquidation-price model in M5 has to use.

Not yet verified: the maximum leverage the account can actually set (the spec assumes 10x). That reads back only once spot margin is on.

## Fee tier

VIP level: "No VIP".

| Quote | Maker | Taker | Round trip, taker both ways |
| --- | --- | --- | --- |
| USDC | 0.100% | 0.050% | 0.10% |
| USDT | 0.100% | 0.100% | 0.20% |

On USDC pairs the maker fee is twice the taker fee, so the planned "limit near mid, reprice after 2 minutes" execution is the expensive route there. M6 should take liquidity on USDC pairs.

For the cost rule (target ≥ 3× fees plus borrow): at 0.10–0.20% round trip the target must be at least 0.3–0.6% away before interest. Borrow interest is small beside that: 4.7% a year is about 0.013% a day on the borrowed part.

## What does not work yet

| Item | Finding |
| --- | --- |
| Host | The key belongs to Bybit global, not Bybit EU. The spec's EU constraints (no perps, cross margin only, 128 USDC/EUR pairs) describe a different venue; the bot keeps to spot and spot margin regardless. `pybit` marks the EU host as institutions-only. |
| Borrowing | Margin mode is `ISOLATED_MARGIN` and spot margin is off. No leveraged long or short is possible until both change. |
| Funds | 0 USDC and practically 0 USDT in the unified account. |
| Shared account | The key is on the master account, which holds 12 other coins. Reconciliation (non-negotiable 8) and equity-based limits would count those holdings. |
| Subaccounts | Not verifiable: the key has no subaccount permission (`10005`). |
| API key | Not IP-bound, so it expires 2027-01-09. Carries Contract, Derivatives and Options trade permissions the bot must never use. |
| Live order self-test | Built, not run. It places one post-only limit buy of about $5.50 on `BTCUSDC` 10% below the bid, confirms it is open, cancels it and confirms it is gone. |
| Margin borrow, close and kill-switch self-test (SPEC §10) | Part of M6. |
| OpenAI model IDs | Not verified in M0; needed from M3. |

## Update 2026-10-09 02:49 UTC: AI subaccount connected

The bot now uses Bybit's **AI Subaccount** `592625865` (`AIsub592625865`), connected through Bybit's OAuth flow (`make bybit-authorize`, see `app/bybit_authorize.py`). Self-test with that key, `--no-x`: 32 checks, 0 failed.

- Isolated from the main account: the key cannot see main balances, withdraw, or change account settings. Transfers and cap limit are governed from the main account's subaccount permissions.
- Margin mode is already **cross** (`REGULAR_MARGIN`) with **spot margin on**, leverage setting 10. Nothing to switch.
- Funded with 6 USDT. **`make selftest-live CONFIRM=yes` passed at 02:53 UTC:** post-only buy of 0.000075 BTC at 74,105.2 (5.56 USDT, 10% below the bid) placed with an `orderLinkId`, seen in the open orders, cancelled, confirmed gone. Nothing filled. M0 is complete.
- Key expires in **89 days** and cannot be IP-bound (OAuth keys don't support it). Renew with `make bybit-authorize` before expiry, or replace with a web-created, IP-bound key once the server exists. The self-test warns from 30 days before expiry.
- The key carries more scopes than spot trade (contract, options, earn, wallet). The bot never calls those endpoints; `BybitClient` is spot-only by construction.

## Needed from the owner

Decided after this run: quote coin is USDT, Telegram is dropped. The tables above show the run made with USDC as quote; the USDT rows are the ones that now matter.

1. **A dedicated subaccount** holding only the bot's capital, with a new API key created on it: spot trade only, no withdrawals, no derivatives, bound to the server IP. Reason: in cross margin, every coin in the account becomes collateral for the bot's borrows, and a bug with spot-trade permission could sell holdings the bot does not manage.
2. **Server IP** (Frankfurt or Amsterdam) for that key binding.
3. On that subaccount: **switch margin mode to cross and turn spot margin on**. The bot can do this through the API if the key is allowed to; say which you prefer.
4. **Fund it** with 10–20 USDT for `make selftest-live CONFIRM=yes`. Shadow mode needs no capital; the real amount goes in before go-live, at least 6 weeks later.
5. Confirm that trading on **Bybit global rather than Bybit EU** is intended. The spec and `docs/DECISIONS.md` named Bybit EU for regulatory reasons; the account is KYC'd in Belgium on the global platform.

M1 (data layer and database) needs none of this and can start now.
