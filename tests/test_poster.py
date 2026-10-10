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


def test_every_template_passes_the_whitelist_with_and_without_a_reason() -> None:
    """A template that the whitelist rejects silently blocks a post (BTC, 2026-10-09)."""
    from decimal import Decimal

    facts = tpl.TradeFacts(**{**FACTS.__dict__, "leverage": Decimal("2.0"), "paper": True})
    with_reason = tpl.TradeFacts(
        **{
            **facts.__dict__,
            "reason": "Polymarket P(up) 26% over 1 day is the strongest bearish reading",
        }
    )
    for i in range(len(tpl.OPEN_TEMPLATES)):
        for f in (facts, with_reason):
            text = tpl.OPEN_TEMPLATES[i].format(
                tag=f.cashtag,
                Tag=f.cashtag,
                direction=f.direction,
                Direction=f.direction.capitalize(),
                entry=tpl.fmt(f.entry),
                lev=f"{float(f.leverage):g}",
                stop=tpl.fmt(f.stop),
                target=tpl.fmt(f.target),
                reason=f" {f.reason}" if f.reason else "",
            )
            assert check(tpl.enforce_marker(text, True)).ok, (i, text, check(text).problems)
    for i in range(len(tpl.CLOSE_TEMPLATES)):
        r = __import__("random").Random(i)
        assert check(
            tpl.render_close(
                tpl.TradeFacts(
                    **{
                        **facts.__dict__,
                        "exit": Decimal("77.6"),
                        "pnl_price_pct": Decimal("0.047"),
                        "pnl_margin_pct": Decimal("0.136"),
                        "holding": "9 hours",
                    }
                ),
                r,
            )
        ).ok
    assert check("Short $BTC at 82,524.9, 2.0x. Stop 84,000, target 78,957.63.").ok


def test_trade_card_renders_a_png_with_the_result() -> None:
    from decimal import Decimal

    from app.social.card import render

    f = tpl.TradeFacts(
        **{
            **FACTS.__dict__,
            "exit": Decimal("77.6"),
            "pnl_price_pct": Decimal("0.047"),
            "pnl_margin_pct": Decimal("0.136"),
            "holding": "9 hours",
            "paper": True,
        }
    )
    png = render(f)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000


async def test_highlight_close_gets_a_card_and_ordinary_closes_do_not() -> None:
    from decimal import Decimal

    from app.config import get_config
    from app.social import poster

    cfg = get_config()
    good = tpl.TradeFacts(
        **{
            **FACTS.__dict__,
            "exit": Decimal("80"),
            "pnl_price_pct": Decimal("0.08"),
            "pnl_margin_pct": Decimal("0.24"),
        }
    )
    meh = tpl.TradeFacts(
        **{
            **FACTS.__dict__,
            "exit": Decimal("75"),
            "pnl_price_pct": Decimal("0.01"),
            "pnl_margin_pct": Decimal("0.03"),
        }
    )
    assert poster.is_highlight("close", good, cfg) and not poster.is_highlight("close", meh, cfg)
    assert not poster.is_highlight("open", good, cfg)
