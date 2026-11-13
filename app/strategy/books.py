"""Which books the slow design runs and on which ledger (track) each one trades.

- ``trend``: the champion, long or cash. On its own paper ledger while it proves the
  plumbing; on ``primary``, the public record, once ``strategy.main_book = "trend"``.
- ``short``: the short leg on paper: eight years say it earns nothing on its own, the last
  month says +10%. It stays a separate book until it earns a place.
- ``challenger``: the three managers' daily call, traded with the champion's structure
  (same slot, same volatility scaling, held until the call changes), so the only
  difference between the two books is who decides the direction.
"""

from __future__ import annotations

from app.config import Config

BOOKS = ("trend", "short", "challenger")
MANAGERS = "managers"  # strategy.main_book while the managers' fast book is the main book
TREND = "trend"


def slow_main(cfg: Config) -> bool:
    """True once the trend book is the main book: the fast book opens nothing new and the
    managers decide once a day."""
    return cfg.strategy.enabled and cfg.strategy.main_book == TREND


def book_tracks(cfg: Config) -> dict[str, str]:
    """book -> track, for the books that are switched on."""
    st = cfg.strategy
    if not st.enabled:
        return {}
    out = {"trend": "primary" if st.main_book == TREND else st.long_track}
    if st.short_track:
        out["short"] = st.short_track
    if st.challenger_track:
        out["challenger"] = st.challenger_track
    return out


def track_books(cfg: Config) -> dict[str, str]:
    """track -> book: the ledgers that belong to a rule book."""
    return {track: book for book, track in book_tracks(cfg).items()}
