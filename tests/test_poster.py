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


def test_shadow_posts_carry_a_marker_and_live_posts_never_do() -> None:
    paper = tpl.TradeFacts(**{**FACTS.__dict__, "paper": True})
    for i in range(5):
        o, c = tpl.render_open(paper, random.Random(i)), tpl.render_close(paper, random.Random(i))
        assert tpl.has_marker(o) and tpl.has_marker(c)
        assert check(o).ok and check(c).ok
    assert not tpl.has_marker(tpl.render_open(FACTS))
    assert (
        tpl.enforce_marker("Closed $SOL at 77.6. just dorking", paper=False)
        == "Closed $SOL at 77.6."
    )
    assert tpl.has_marker(tpl.enforce_marker("Closed $SOL at 77.6.", paper=True))
    # The writer's own phrasing counts as the marker: nothing is appended on top.
    own = "Short $ETH at 2489.28, 1x. Paper trade, so nobody's rent depends on it."
    assert tpl.enforce_marker(own, paper=True) == own
    assert tpl.has_marker("Still in shadow mode, so take it with salt.")
    assert not tpl.has_marker("Closed $SOL at 77.6. Longer hold than I wanted.")
    assert not tpl.has_marker("Tested the support twice.")  # "test" alone is not a marker


def test_holding_text() -> None:
    assert tpl.holding_text(0.5) == "30 minutes"
    assert tpl.holding_text(9.2) == "9 hours"
    assert tpl.holding_text(72) == "3 days"
    assert tpl.holding_text(60) == "2.5 days"
