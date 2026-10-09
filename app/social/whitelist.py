"""Number whitelist for everything the bot publishes (SPEC §11, §12 M8a).

Only prices, leverage, percentages and durations may appear as numbers. Anything else
(amounts, balances, sizes, counts that look like money, IDs) fails. The same check runs
on X posts and on the public-site snapshot text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NUMBER = re.compile(
    r"(?<![A-Za-z0-9@#_/.])[-+]?\$?\d[\d,]*(?:\.\d+)?(?:k|K|M|B)?(?![A-Za-z0-9_@#])"
)
# What may follow or precede a number for it to count as an allowed kind.
PCT = re.compile(r"^[-+]?\d[\d,]*(?:\.\d+)?%$")
LEVERAGE = re.compile(r"^\d+(?:\.\d+)?x$", re.I)
LEVERAGE_AHEAD = re.compile(r"^\d+(?:\.\d+)?x(?![A-Za-z0-9])", re.I)  # "2.0x." seen from "2"
DURATION = re.compile(
    r"^\d+(?:\.\d+)?\s?(?:m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)\b", re.I
)
PRICE_CONTEXT = re.compile(
    r"\b(?:at|entry|stop|target|exit|closed at|took|from|to|for|above|below|under|over|near|"
    r"around|@)\s*$",
    re.I,
)
URL = re.compile(r"https?://|www\.|\.(?:com|io|xyz|dev|net|org)\b", re.I)
CASHTAG = re.compile(r"\$[A-Z]{2,10}\b")
FORBIDDEN_WORDS = re.compile(
    r"\b(?:usdt|usdc|usd|dollars?|euros?|€|balance|position size|size|notional|margin call|"
    r"account|subaccount|wallet|api|bot|automated|algorithm|buy now|guaranteed|will go to|"
    r"financial advice|nfa|dyor|airdrop|giveaway)\b",
    re.I,
)
ADVICE = re.compile(
    r"\b(?:buy|sell|long|short)\s+(?:now|this|it)\b|\bwill\s+(?:pump|moon|hit)\b", re.I
)


@dataclass(frozen=True)
class WhitelistResult:
    ok: bool
    problems: list[str]


def _kind(token: str, before: str, after: str) -> str | None:
    if PCT.match(token + after[:1]) or PCT.match(token):
        return "percentage"
    # "2x", and "2.0x"/"2.25x", where the number token stops before the decimals
    if (
        LEVERAGE.match(token + after[:1])
        or LEVERAGE.match(token)
        or LEVERAGE_AHEAD.match(token + after[:5])
    ):
        return "leverage"
    if DURATION.match(token + after[:9]):
        return "duration"
    if PRICE_CONTEXT.search(before) or token.startswith("$"):
        return "price"
    return None


SITE_ALLOWED = {"nfa", "bot"}  # the public site is openly a bot; X posts never say so


def check(text: str, *, site: bool = False) -> WhitelistResult:
    problems: list[str] = []
    if URL.search(text):
        problems.append("contains a link")
    clean = CASHTAG.sub("", text)  # cashtags are not numbers
    for m in NUMBER.finditer(clean):
        token = m.group(0)
        before, after = clean[: m.start()], clean[m.end() :]
        kind = _kind(token, before, after)
        if kind is None:
            problems.append(f"number without allowed context: {token!r}")
    for w in FORBIDDEN_WORDS.finditer(text):
        if site and w.group(0).lower() in SITE_ALLOWED:
            continue
        problems.append(f"forbidden word: {w.group(0)!r}")
    if ADVICE.search(text):
        problems.append("advice language")
    return WhitelistResult(not problems, problems)
