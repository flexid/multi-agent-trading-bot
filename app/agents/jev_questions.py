"""The Jev question sets, versioned like prompts (bump ``VERSION`` when a question changes).

Jev reads literally (its docs: write the exact condition, put boundary cases in the
criteria, keep arithmetic and dates in code), so every question names the field it judges
and every option carries a rubric. Jev only classifies; thresholds and scoring stay in code.
"""

from __future__ import annotations

from typing import Any

VERSION = 1

EVENT_TYPES: dict[str, str] = {
    "listing": "a new exchange listing or trading pair, new margin or borrow support, "
    "an ETF or index inclusion for the asset",
    "delisting": "removal of a trading pair, of margin or borrow support, or a trading "
    "suspension for the asset",
    "unlock": "a token unlock, vesting release or large transfer of locked tokens",
    "hack": "an exploit, hack, drained funds, a critical bug or an emergency pause",
    "partnership": "a partnership, integration, major product launch or protocol upgrade",
    "other": "none of the above: marketing, giveaways, recaps, opinions, unrelated news",
}
DIRECTIONS: dict[str, str] = {
    "bullish": "likely to push the asset's price up over the next days",
    "bearish": "likely to push the asset's price down over the next days",
    "neutral": "no clear effect on the asset's price",
}
STANCES: dict[str, str] = {
    "bullish": "expects or argues for a higher price of the asset",
    "bearish": "expects or argues for a lower price of the asset",
    "neutral": "no view on the price, or balanced",
}
KINDS: dict[str, str] = {
    "news": "reports a verifiable event: a listing, hack, ETF, regulation, outage, unlock, "
    "partnership",
    "analysis": "charts, price levels, on-chain data, reasoning about the asset",
    "shill": "promotion, hype, price promises, giveaways, engagement bait",
    "other": "none of the above",
}
CREDIBILITY_LEVELS: list[str] = [
    "anonymous hype or a price promise with no checkable claim",
    "an opinion with some reasoning but no source or data",
    "a specific, checkable claim or data from an identifiable source",
    "an official announcement or a primary source with specifics",
]


def asset_criteria(assets: dict[str, str]) -> dict[str, str]:
    """``assets``: ticker -> plain-language description. Adds the no-match option."""
    return {**assets, "none": "none of these assets, or another coin, index or stock"}


def event_questions(assets: dict[str, str]) -> dict[str, Any]:
    """Four questions over one event ``{"source": ..., "text": ..., "published_at": ...}``."""
    from typesafe_sdk import Choice, Noul

    return {
        "asset": Choice(
            instructions="Which asset is the event in `text` mainly about?",
            criteria=asset_criteria(assets),
        ),
        "type": Choice(
            instructions="What kind of event does `text` describe for that asset?",
            criteria=EVENT_TYPES,
        ),
        "direction": Choice(
            instructions="What is the likely effect of the event in `text` on that asset's "
            "price over the next days?",
            criteria=DIRECTIONS,
        ),
        "material": Noul(
            instructions="Would a trader expect the event in `text` to move that asset's "
            "price by more than 3% within 48 hours?",
            criteria={
                "true": "a trader would reposition on this event",
                "false": "routine marketing, recap, giveaway or minor news; no reposition",
            },
        ),
    }


def post_questions(count: int, assets: dict[str, str]) -> dict[str, Any]:
    """Five questions per post over ``{"posts": [{"text": ...}, ...]}``: asset, stance,
    kind, credibility (4 ordered levels) and shock. ``count`` posts, keys ``p{i}_*``."""
    from typesafe_sdk import Choice, Noul, Score

    questions: dict[str, Any] = {}
    for i in range(count):
        ref = f"`posts[{i}].text`"
        questions[f"p{i}_asset"] = Choice(
            instructions=f"Which asset is the post {ref} mainly about?",
            criteria=asset_criteria(assets),
        )
        questions[f"p{i}_stance"] = Choice(
            instructions=f"What is the stance of the post {ref} toward that asset's price?",
            criteria=STANCES,
        )
        questions[f"p{i}_kind"] = Choice(
            instructions=f"What kind of post is {ref}?", criteria=KINDS
        )
        questions[f"p{i}_cred"] = Score(
            instructions=f"How credible is the post {ref} as information about the asset?",
            criteria=CREDIBILITY_LEVELS,
        )
        questions[f"p{i}_shock"] = Noul(
            instructions=f"Does the post {ref} report news that would move the asset's price "
            "within hours: an exchange hack, a delisting, major regulatory action, a protocol "
            "exploit, or a large ETF approval or rejection?",
            criteria={
                "true": "it reports such news as happening or just announced",
                "false": "routine news, analysis, opinion, hype, or no news at all",
            },
        )
    return questions
