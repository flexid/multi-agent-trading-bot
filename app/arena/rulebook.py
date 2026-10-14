"""A trader's rulebook: the strategy its model writes once per round (bot spec §3), and its
compilation into Jev questions (§5). The rulebook is public; nothing in it is an order."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

QUESTIONS_VERSION = 4  # 4: facts computed by code beside the candles (2026-10-14)
CONVICTION_LEVELS = [  # the score question's rubric; Jev's score is the level index, /4
    "the rule is not met, or only with a generous reading",
    "the rule is partly met; more than one condition is borderline or missing",
    "the rule is mostly met; one condition is borderline",
    "the rule is met; every condition is visible in the data",
    "a textbook example: every condition is clearly and strongly met",
]

Leverage = Field(ge=1, le=10)


class Rulebook(BaseModel):
    """Exactly what the strategy author returns; validated, never trusted beyond that.
    The rules engine clamps the stop and refuses pairs the fees make impossible."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    thesis: str = Field(min_length=1, max_length=400)
    leverage: int = Field(ge=1, le=10)
    tp_net_roe_pct: int = Field(ge=2, le=10)
    sl_net_roe_pct: float = Field(gt=0, le=10)  # clamped to tp by the engine
    min_conviction: float = Field(ge=0.5, le=0.95)
    entry_long: str = Field(min_length=1, max_length=400)
    entry_short: str = Field(min_length=1, max_length=400)
    no_trade: str = Field(min_length=1, max_length=400)
    early_exit: str | None = Field(default=None, max_length=400)

    def public(self) -> dict[str, Any]:
        """The site's shape (arena-site/README.md)."""
        return {
            "name": self.name,
            "thesis": self.thesis,
            "leverage": self.leverage,
            "tp_net_roe_pct": self.tp_net_roe_pct,
            "sl_net_roe_pct": self.sl_net_roe_pct,
            "min_conviction": self.min_conviction,
            "entry_long": self.entry_long,
            "entry_short": self.entry_short,
            "no_trade": self.no_trade,
            "early_exit": self.early_exit,
        }


def compile_questions(rb: Rulebook, *, side: str | None = None) -> dict[str, Any]:
    """The Jev question set for one rulebook. Flat: ``signal`` and ``conviction``. In a
    position (``side`` given) and only when the rulebook has an early exit: ``early_exit``.
    Jev judges literally and prefers "none"; leverage and targets never come from Jev."""
    from typesafe_sdk import Choice, Noul, Score

    if side is None:
        return {
            "signal": Choice(
                instructions=(
                    "You are judging whether the current BTC market satisfies a trader's entry "
                    "rules. `facts` holds observations computed exactly by code for each "
                    "timeframe (1m, 5m, 15m, 1h): the last candle, where its close sits "
                    "against the prior 5, 10 and 20 candles, volume against the average, runs "
                    "of closes, ranges, and the most recent 15m breakout with what the 1m "
                    "candles did since. `candles` holds the raw data ([minutes before now, "
                    "open, high, low, close, volume], oldest first). Check each condition of a "
                    "rule against the facts first and the candles second. Judge literally. "
                    'Prefer "none" unless every condition of a rule is met.'
                ),
                criteria={
                    "long": f"Open a LONG when: {rb.entry_long}",
                    "short": f"Open a SHORT when: {rb.entry_short}",
                    "none": (
                        f"Stay flat when: {rb.no_trade}. Also stay flat if neither entry rule "
                        "is clearly met."
                    ),
                },
            ),
            "conviction": Score(
                instructions=(
                    "How clearly is the chosen entry rule satisfied by the candle data? "
                    "Judge the rule you chose; if you chose none, the lowest level."
                ),
                criteria=CONVICTION_LEVELS,
            ),
        }
    if not rb.early_exit:
        return {}
    return {
        "early_exit": Noul(
            instructions=(
                f"The trader has an open {side.upper()} position on BTC. `candles` holds the "
                "latest candle data; `position` holds the entry price, the unrealised return "
                "on margin in percent and the minutes the position has been open. Should the "
                "position be closed now?"
            ),
            criteria={
                "true": f"Close now when: {rb.early_exit}",
                "false": "Otherwise hold and let the take-profit or stop-loss decide.",
            },
        )
    }
