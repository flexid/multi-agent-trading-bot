"""Logic version: bumped when the decision logic changes in a way that makes earlier
decisions not comparable. Stored on every cycle row.

3  2026-10-10  Orak v3: range buckets as a full distribution, volume floor $200 with
              volume weighting, horizons weighted 0.3/0.4/0.3, a 4h shift beside the 24h
              one, year-end markets as evidence for the PMs.
2  2026-10-10  universe BTC, ETH, SOL, XRP, DOGE (BNB and SPX6900 dropped); depth-based
              leverage cap per asset replaces the SPX6900 cap; correlation-aware exposure
              cap (beta-weighted, 1.5× equity); soft/hard stops with the level buffer.
1  2026-10-08  initial logic (SPEC §6-§9).
"""

LOGIC_VERSION = 3
