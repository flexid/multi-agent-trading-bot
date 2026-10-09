"""Fallback posts when the writer or the audit fails (SPEC §11). Filled from the trade
record only; every template passes the number whitelist by construction."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class TradeFacts:
    cashtag: str  # "$SOL"
    direction: str  # long | short
    entry: Decimal
    leverage: Decimal
    stop: Decimal
    target: Decimal
    exit: Decimal | None = None
    pnl_price_pct: Decimal | None = None
    pnl_margin_pct: Decimal | None = None
    holding: str | None = None  # "9 hours", "2 days"
    reason: str | None = None  # short, already sanitized
    paper: bool = False


SHADOW_MARKERS = ("dorking...", "just dorking", "still dorking around")
# The writer may say it in its own words instead ("Paper trade, so nobody's rent depends
# on it"); any of these counts as the marker, so nothing gets appended on top (owner,
# 2026-10-09: "don't put 'just dorking' everywhere").
NATURAL_MARKER = re.compile(
    r"\b(paper|shadow|dork\w*|test[- ](trade|run|mode)|play money|not real money)\b", re.I
)


def has_marker(text: str) -> bool:
    return any(m in text for m in SHADOW_MARKERS) or NATURAL_MARKER.search(text) is not None


def with_marker(text: str, rng: random.Random | None = None) -> str:
    """Append a test-mode marker to a shadow post (owner, 2026-10-09)."""
    if has_marker(text):
        return text
    rng = rng or random.Random()
    return f"{text.rstrip()} {rng.choice(SHADOW_MARKERS)}"


def enforce_marker(text: str, paper: bool, rng: random.Random | None = None) -> str:
    """Shadow posts always carry a marker; live posts never do. Code, not the model."""
    if paper:
        return with_marker(text, rng)
    for m in SHADOW_MARKERS:
        text = text.replace(f" {m}", "").replace(m, "")
    return text.strip()


def fmt(x: Decimal) -> str:
    s = f"{x:.6g}" if x < 10 else f"{x:,.2f}".rstrip("0").rstrip(".")
    return s


def pct(x: Decimal) -> str:
    return f"{x * 100:+.1f}%"


OPEN_TEMPLATES = [
    "{Tag} {direction} at {entry}, {lev}x. Stop {stop}, target {target}.{reason}",
    "Took a {tag} {direction} at {entry}, {lev}x. Stop {stop}, target {target}.{reason}",
    "{Direction} {tag} from {entry} with {lev}x. Stop at {stop}, looking for {target}.{reason}",
    "In {tag} {direction} at {entry}. {lev}x, stop {stop}, target {target}.{reason}",
    "{Tag}: {direction} at {entry}, {lev}x, stop {stop}, target {target}. Let's see.{reason}",
    "Small {tag} {direction} at {entry}, {lev}x. Stop {stop}, target {target}.{reason}",
    "{Tag} {direction}, entry {entry}, {lev}x. Stop {stop}, target {target}.{reason}",
    "New position: {tag} {direction} at {entry}, {lev}x. Stop {stop}, target {target}.{reason}",
    "{Direction} {tag} at {entry}, {lev}x. Out at {stop} if wrong, at {target} if right.{reason}",
    "{Tag} {direction} at {entry}, {lev}x. Stop {stop}, target {target}. Nothing fancy.{reason}",
]
CLOSE_TEMPLATES = [
    "Closed {tag} at {exit} after {hold}. {pp} on price, {pm} on margin.",
    "Out of {tag} at {exit}. {pp} on price, {pm} on margin, {hold}.",
    "{Tag} closed at {exit}, {hold} in. {pp} on price, {pm} on margin.",
    "Done with {tag} at {exit}. {pp} on price, {pm} on margin after {hold}.",
    "{Tag}: out at {exit}. {pp} on price, {pm} on margin. {hold}, that's it.",
    "Closed the {tag} at {exit}. {pp} on price, {pm} on margin. {hold}.",
    "{Tag} out at {exit} after {hold}. {pp} on price and {pm} on margin.",
    "Took the {tag} off at {exit}. {pp} on price, {pm} on margin, {hold} held.",
    "{Tag} closed at {exit}. {pp} on price, {pm} on margin. {hold}.",
    "Flat {tag} at {exit}. {pp} on price, {pm} on margin over {hold}.",
]


def render_open(f: TradeFacts, rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    reason = f" {f.reason}" if f.reason else ""
    text = rng.choice(OPEN_TEMPLATES).format(
        tag=f.cashtag,
        Tag=f.cashtag,
        direction=f.direction,
        Direction=f.direction.capitalize(),
        entry=fmt(f.entry),
        lev=f"{float(f.leverage):g}",  # Decimal("2.0") would print 2.0; 2x reads better
        stop=fmt(f.stop),
        target=fmt(f.target),
        reason=reason,
    )
    return enforce_marker(text, f.paper, rng)


def render_close(f: TradeFacts, rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    assert f.exit is not None and f.pnl_price_pct is not None and f.pnl_margin_pct is not None
    text = rng.choice(CLOSE_TEMPLATES).format(
        tag=f.cashtag,
        Tag=f.cashtag,
        exit=fmt(f.exit),
        hold=f.holding or "a while",
        pp=pct(f.pnl_price_pct),
        pm=pct(f.pnl_margin_pct),
    )
    return enforce_marker(text, f.paper, rng)


def holding_text(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)} minutes"
    if hours < 48:
        return f"{int(round(hours))} hours"
    return f"{hours / 24:.1f} days".replace(".0 days", " days")
