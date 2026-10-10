"""Logic version: bumped when the decision logic changes in a way that makes earlier
decisions not comparable. Stored on every cycle row.

2  2026-10-10  universe BTC, ETH, SOL, XRP, DOGE (BNB and SPX6900 dropped); depth-based
              leverage cap per asset replaces the SPX6900 cap; correlation-aware exposure
              cap (beta-weighted, 1.5× equity); soft/hard stops with the level buffer.
1  2026-10-08  initial logic (SPEC §6-§9).
"""

LOGIC_VERSION = 2
