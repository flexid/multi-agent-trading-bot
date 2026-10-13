"""The dorkbot arena: three AI traders on the BTCUSDT perpetual under fixed rules.

Spec: the owner's TRADING_COMPETITION_SPEC.md (bot) and COMPETITION_SITE_SPEC.md (site,
built in arena-site/). Built in Python inside this repo (owner 2026-10-13), reusing the
Bybit client, the LLM layer, the Jev client and the ledger database. Its own process
(`python -m app.arena.main`), never imported by the executor or the scheduler.

Layout: rulebook (the strategy a model writes), rules (fee maths, bench and round logic:
pure functions), candles (the only input), exchange (the linear adapter), author (slow
brain), reflex (fast brain, Jev), ledger (rows), engine (the minute loop), api (read-only).
"""
