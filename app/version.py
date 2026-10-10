"""Logic version: bumped when the decision logic changes in a way that makes earlier
decisions not comparable. Stored on every cycle row.

5  2026-10-10  alt sleeve only: the Polymarket agent is replaced by the catalyst agent
              (Djaf, TypeSafe Jev): project and alt-news X accounts, Bybit announcements
              and the unlock calendar, classified by Jev, scored with time decay in code;
              Jev also labels the alt sleeve's X posts. Sonnet runs beside it in shadow.
              Bluechips unchanged. The 2026-11-07 review date stands.
4  2026-10-10  two sleeves: bluechips (BTC, ETH, SOL, XRP, DOGE; 70% of capital, 1% risk,
              leverage to 10x) and alts (PEPE, HBAR, PUMP, SPX6900, ENA; 30%, 0.5% risk,
              3x, depth caps, Polymarket weight ~0, X and charts up, Sonnet vision). Each
              sleeve has its own day-loss stop, exposure cap and go-live evaluation; the
              beta cap also applies across both. FROZEN until the 4-week review
              (2026-11-07): no decision-logic change before then.
3  2026-10-10  Orak v3: range buckets as a full distribution, volume floor $200 with
              volume weighting, horizons weighted 0.3/0.4/0.3, a 4h shift beside the 24h
              one, year-end markets as evidence for the PMs.
2  2026-10-10  universe BTC, ETH, SOL, XRP, DOGE (BNB and SPX6900 dropped); depth-based
              leverage cap per asset replaces the SPX6900 cap; correlation-aware exposure
              cap (beta-weighted, 1.5× equity); soft/hard stops with the level buffer.
1  2026-10-08  initial logic (SPEC §6-§9).
"""

LOGIC_VERSION = 5
FROZEN_UNTIL = "2026-11-07"  # the 4-week review; no logic change before then
