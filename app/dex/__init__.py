"""dorkbot on a DEX (docs/DEX_PLAN.md, owner 2026-10-19): a mirror of the main book's record
on Hyperliquid perpetuals, for a wallet (later a vault) whose every trade is on-chain.

No decision logic lives here. The mirror follows what the storyline track did: it opens when
the main book opened, at the plan's leverage and the plan's stop and target distances from its
own fill, and closes when the main book closed. The exchange holds the stop and the take as
trigger orders as a safety net. Modes: paper (simulated fills on the DEX's prices), testnet,
live. Its own process; it never touches the executor or the scheduler.
"""
