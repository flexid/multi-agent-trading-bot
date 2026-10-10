"""The crew: display names for every agent and process (owner, 2026-10-09). Internal ids
stay as they are in code, prompts and the database; these names are for the site, the
admin and the docs. No digits in the blurbs: they pass through the site whitelist."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Member:
    id: str
    name: str
    role: str
    blurb: str


CREW: list[Member] = [
    Member(
        "indicators",
        "Cassette",
        "reads the tape",
        "Reads the tape: trend, momentum, volatility, funding and open interest. Pure math, "
        "no opinions, summarized by a small model into plain words.",
    ),
    Member(
        "chart_patterns",
        "Squint",
        "reads the charts",
        "Squints at the charts for flags, wedges, ranges and breakouts: code finds the "
        "shapes first, a vision model gets a second look at the picture.",
    ),
    Member(
        "polymarket",
        "Orak",
        "reads the prediction markets",
        "Where the crowd's money says price will be, how those odds shifted in a day, and "
        "which way the up-or-down bets lean. Counts for the bluechips; the alts are too thin "
        "for it.",
    ),
    Member(
        "macro",
        "Storm",
        "reads the climate",
        "The climate outside crypto: rates, the dollar, equities, plus the crypto-native "
        "mood (fear and greed, bitcoin dominance) and how coupled crypto is to it right now.",
    ),
    Member(
        "x_sentiment",
        "Ears",
        "listens to X",
        "Listens to X: who is loud, who is credible, what they lean, and how many people are "
        "talking compared with a normal day. Crowded one-way chatter makes it suspicious.",
    ),
    Member(
        "catalysts",
        "Djaf",
        "spots catalysts (alts only)",
        "The instant decision maker for the alt sleeve: project accounts, exchange notices "
        "and the unlock calendar, labelled in a blink. Code turns the labels into a score.",
    ),
    Member(
        "optima",
        "Optima",
        "weekly efficiency check, advisory",
        "The weekly efficiency check: which readings earn their keep, how clean the fills "
        "are, and whether the spend stays on track. Advises only; changes nothing.",
    ),
    Member(
        "pm_1",
        "Dyne",
        "portfolio manager (Claude)",
        "Turns the readings into a trade plan with entry, stop and target. Never sees what "
        "the other manager said.",
    ),
    Member(
        "pm_2",
        "Dork",
        "portfolio manager (GPT)",
        "The second opinion, same evidence, different brain. A trade needs both managers to "
        "agree on the direction; one flat means no trade.",
    ),
    Member(
        "leverage",
        "Dial",
        "sets the size",
        "Sets the size: risk per trade divided by the stop distance, then the lowest of every "
        "cap (volatility, events, thin books, disagreement, losing streaks).",
    ),
    Member(
        "risk",
        "Bouncer",
        "the risk rules",
        "Deterministic rules, no model: exposure limits, day-loss lock, drawdown pause, the "
        "brake. The only one who can say no.",
    ),
    Member(
        "executor",
        "Hands",
        "works the exchange",
        "The only process that touches the exchange. Places entries, runs stops, targets, "
        "trailing stops and time-stops in code, around the clock, independent of any model.",
    ),
    Member(
        "post_writer",
        "Scribe",
        "writes the posts",
        "Writes the X post after a fill, in the owner's voice, from the trade record only.",
    ),
    Member(
        "post_auditor",
        "Pedant",
        "checks the posts",
        "Checks every post against the hard rules before code double-checks the numbers.",
    ),
    Member(
        "memo",
        "Coach",
        "weekly review",
        "Reads the week's facts and writes the Monday memo with proposals for the owner. "
        "Proposes, never changes.",
    ),
]
BY_ID = {m.id: m for m in CREW}


def display(agent_id: str) -> str:
    """'Cassette (indicators)' for known ids, the id itself otherwise."""
    m = BY_ID.get(agent_id)
    return f"{m.name} ({agent_id})" if m else agent_id
