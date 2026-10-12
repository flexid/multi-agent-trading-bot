"""Logic version: bumped when the decision logic changes in a way that makes earlier
decisions not comparable. Stored on every cycle row.

9  2026-10-12  no chasing: the risk engine blocks a call in the direction of a 24-hour move
              larger than `risk.chase_max_atr` (2) ATR, and the managers see the 4h and
              24h change and the day's move in ATR in the pack with an instruction not to
              chase (pm prompt v9). Three days of record: 42 non-flat calls, a quarter
              right on the 24h move, every one extending the move that had just happened.
              Owner's decision.
8  2026-10-11  a sleeve's day-loss stop still closes every position in the sleeve, but
              locks new trades until 00:00 UTC only in the direction that lost; a further
              loss of the same size locks the whole sleeve. Owner's decision.
7  2026-10-11  the conviction bar for borrowing (and so for every short) goes from 0.5
              to 0.4: with the lowest conviction of the agreeing managers counting, 0.5
              blocked nearly every short. Owner's decision.
6  2026-10-11  three managers, two of three decide: Daerm (Grok 4.7) takes a third seat.
              A trade needs at least two managers calling the same direction and nobody
              the opposite one; all agreeing = full size, two with the third flat = half
              size. A manager without an answer (or the third past its deadline) is left
              out and the other two must agree. Owner's decision during the freeze; the
              old two-of-two rule is logged beside it for the review.
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

LOGIC_VERSION = 9
FROZEN_UNTIL = "2026-11-07"  # the 4-week review; no logic change before then
