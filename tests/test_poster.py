import random
from decimal import Decimal as D

from app.social import templates as tpl
from app.social.whitelist import check

FACTS = tpl.TradeFacts(
    "$SOL",
    "long",
    D("74.10"),
    D(3),
    D("72.30"),
    D(78),
    D("77.60"),
    D("0.047"),
    D("0.136"),
    "9 hours",
    "range finally broke",
)


def test_every_open_template_passes_the_whitelist() -> None:
    for i in range(len(tpl.OPEN_TEMPLATES)):
        text = tpl.render_open(FACTS, random.Random(i))
        assert check(text).ok, (text, check(text).problems)
        assert "$SOL" in text and "3x" in text


def test_every_close_template_passes_the_whitelist() -> None:
    for i in range(len(tpl.CLOSE_TEMPLATES)):
        text = tpl.render_close(FACTS, random.Random(i))
        assert check(text).ok, (text, check(text).problems)
        assert "+4.7%" in text and "+13.6%" in text and "9 hours" in text


def test_paper_posts_are_labelled() -> None:
    paper = tpl.TradeFacts(**{**FACTS.__dict__, "paper": True})
    assert tpl.render_open(paper).startswith("Paper: ")
    assert tpl.render_close(paper).startswith("Paper: ")


def test_holding_text() -> None:
    assert tpl.holding_text(0.5) == "30 minutes"
    assert tpl.holding_text(9.2) == "9 hours"
    assert tpl.holding_text(72) == "3 days"
    assert tpl.holding_text(60) == "2.5 days"
