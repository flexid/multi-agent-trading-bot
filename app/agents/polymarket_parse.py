"""Deterministic parser for Polymarket crypto questions (owner briefing 2026-10-09).

Polymarket's crypto markets are generated from a handful of templates, so a parser maps
almost all of them without a model. What it cannot parse but does mention one of our
assets goes to the model mapper; what mentions none is "other" and never costs a call.

Kinds:
- ``price``: one threshold, ``direction`` "above" (close above / reach / hit) or "below"
  (close below / dip to / fall to). One-touch and close-based markets are both P(≥ X)
  points on the ladder; the agent does not distinguish them.
- ``range``: "between $A and $B" (stored, not used by the ladder).
- ``updown``: "Up or Down" markets with their window in minutes. Windows under an hour
  are noise (SPEC §6) and are not even stored.
- ``other``: no asset we trade, or a non-price question about one.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

ASSET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SP500", re.compile(r"s&p\s?500|\bsp500\b", re.I)),
    ("BTC", re.compile(r"\b(bitcoin|btc)\b", re.I)),
    ("ETH", re.compile(r"\b(ethereum|eth)\b", re.I)),
    ("SOL", re.compile(r"\b(solana|sol)\b", re.I)),
    ("XRP", re.compile(r"\b(xrp|ripple)\b", re.I)),
    ("DOGE", re.compile(r"\b(dogecoin|doge)\b", re.I)),
    ("BNB", re.compile(r"\bbnb\b", re.I)),  # no longer traded; still parsed for the record
    ("SPX6900", re.compile(r"\b(spx6900|spx)\b", re.I)),
]
NAME = (
    r"(?P<asset>bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|dogecoin|doge|bnb|spx6900|spx|"
    r"s&p\s?500(?:\s\(spx\))?)"
)
MONEY = r"\$?\s?(?P<n>\d[\d,]*(?:\.\d+)?)\s?(?P<unit>[kKmM])?"
MONEY2 = MONEY.replace("(?P<n>", "(?P<n2>").replace("(?P<unit>", "(?P<unit2>")

PRICE_OF = re.compile(
    rf"^will the price of {NAME} be (?P<rel>above|greater than|below|less than) {MONEY}\b",
    re.I,
)
PRICE_BETWEEN = re.compile(rf"^will the price of {NAME} be between {MONEY} and {MONEY2}\b", re.I)
TOUCH = re.compile(
    rf"^will {NAME} (?P<verb>reach|hit|dip to|fall to|drop to|fall below) {MONEY}\b", re.I
)
SP_CLOSE = re.compile(rf"^will {NAME} close at (?P<rel>[<>])?{MONEY}(?:\s?-\s?{MONEY2})?\b", re.I)
UPDOWN_Q = re.compile(rf"^{NAME} up or down\b(?P<rest>.*)$", re.I)
UPDOWN_SLUG = re.compile(r"updown-(?P<n>\d+)(?P<unit>[mh])-", re.I)
CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?\s?(AM|PM)", re.I)

MIN_UPDOWN_WINDOW_MIN = 60


@dataclass(frozen=True)
class Parsed:
    asset: (
        str | None
    )  # BTC, ETH, SOL, XRP, DOGE (BNB, SPX6900 for the record), SP500; None = none of ours
    kind: str  # price | range | updown | other
    threshold: float | None = None
    direction: str | None = None  # above | below
    low: float | None = None
    high: float | None = None
    window_min: int | None = None

    def mapping(self) -> dict[str, object]:
        out = {k: v for k, v in asdict(self).items() if v is not None and k != "asset"}
        out["source"] = "parser"
        return out


def _asset(text: str) -> str | None:
    for asset, pat in ASSET_PATTERNS:
        if pat.search(text):
            return asset
    return None


def _money(n: str, unit: str | None) -> float:
    value = float(n.replace(",", ""))
    if unit and unit.lower() == "k":
        value *= 1_000
    elif unit and unit.lower() == "m":
        value *= 1_000_000
    return value


def _minutes(hour: str, minute: str | None, ampm: str) -> int:
    h = int(hour) % 12 + (12 if ampm.upper() == "PM" else 0)
    return h * 60 + int(minute or 0)


def _updown_window(question: str, slug: str, rest: str) -> int | None:
    m = UPDOWN_SLUG.search(slug)
    if m:
        return int(m.group("n")) * (60 if m.group("unit").lower() == "h" else 1)
    clocks = CLOCK.findall(rest)
    if len(clocks) >= 2:  # "October 9, 8:00AM-12:00PM ET"
        start, end = (_minutes(*c) for c in clocks[:2])
        return (end - start) % (24 * 60) or 24 * 60
    if len(clocks) == 1:  # "October 9, 4AM ET": one hour
        return 60
    if re.search(r"\bon\s+[A-Za-z]+\s+\d", rest):  # "on October 9?": one day
        return 24 * 60
    return None


def parse(question: str, slug: str = "") -> Parsed | None:
    """A mapping for ``question``; None when it mentions one of our assets in a form the
    parser does not know (the model mapper takes those)."""
    q = " ".join(question.split())
    asset = _asset(q)
    if asset is None:
        return Parsed(None, "other")
    if m := UPDOWN_Q.match(q):
        window = _updown_window(q, slug, m.group("rest"))
        return Parsed(asset, "updown", window_min=window) if window else None
    if m := PRICE_OF.match(q):
        above = m.group("rel").lower() in ("above", "greater than")
        return Parsed(
            asset, "price", _money(m.group("n"), m.group("unit")), "above" if above else "below"
        )
    if m := PRICE_BETWEEN.match(q):
        lo, hi = _money(m.group("n"), m.group("unit")), _money(m.group("n2"), m.group("unit2"))
        return Parsed(asset, "range", low=min(lo, hi), high=max(lo, hi))
    if m := TOUCH.match(q):
        above = m.group("verb").lower() in ("reach", "hit")
        return Parsed(
            asset, "price", _money(m.group("n"), m.group("unit")), "above" if above else "below"
        )
    if asset == "SP500" and (m := SP_CLOSE.match(q)):
        if m.group("n2"):
            lo, hi = _money(m.group("n"), m.group("unit")), _money(m.group("n2"), m.group("unit2"))
            return Parsed(asset, "range", low=min(lo, hi), high=max(lo, hi))
        if m.group("rel"):
            direction = "above" if m.group("rel") == ">" else "below"
            return Parsed(asset, "price", _money(m.group("n"), m.group("unit")), direction)
    return None


def short_updown(question: str, slug: str = "") -> bool:
    """True for 5- and 15-minute Up/Down markets: not stored at all."""
    p = parse(question, slug)
    return (
        p is not None
        and p.kind == "updown"
        and p.window_min is not None
        and p.window_min < MIN_UPDOWN_WINDOW_MIN
    )
