import random
from decimal import Decimal as D

import pytest

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


def test_a_close_post_replies_to_the_opener_that_went_out() -> None:
    """Two "open" rows for one position (a blocked draft and the posted retry) must not
    break the close post: it replies to the one with an X id."""
    from datetime import UTC, datetime

    from sqlalchemy import delete

    from app.db.models import XPostOut
    from app.db.session import new_session
    from app.social.poster import opener_x_id
    from tests.test_data_layer import _db_available

    if not _db_available():
        return
    now = datetime.now(UTC)
    with new_session() as s:
        s.execute(delete(XPostOut).where(XPostOut.position_id == 987654))
        common = dict(
            position_id=987654,
            kind="open",
            text="t",
            source="writer",
            audit_ok=True,
            scheduled_at=now,
            dry_run=False,
            created_at=now,
        )
        s.add(XPostOut(**common, whitelist_ok=False, x_id=None, error="blocked by whitelist"))
        s.commit()
        assert opener_x_id(s, 987654) is None
        s.add(XPostOut(**common, whitelist_ok=True, x_id="111"))
        s.commit()
        assert opener_x_id(s, 987654) == "111"
        s.execute(delete(XPostOut).where(XPostOut.position_id == 987654))
        s.commit()


def test_posting_events_setting_is_read() -> None:
    """Owner 2026-10-15: closes only by default; opens and losing closes are configurable."""
    from app.config import PostingConfig, get_config

    assert get_config().posting.events == "closes"
    assert PostingConfig.model_fields["events"].default == "closes"


def test_sentences_do_not_split_on_decimals() -> None:
    from app.social.poster import sentences

    out = sentences("Closed $DOGE at 0.08195 after 15 hours. Paper trade, so nobody noticed.")
    assert "closed $doge at 0.08195 after 15 hours." in out
    assert not any(s.endswith(" 0.") for s in out)


def test_a_draft_with_the_shape_of_a_recent_post_is_caught() -> None:
    """Owner 2026-10-24: X flagged the account for near-identical posts. Same skeleton,
    other numbers and cashtag, is still the same post."""
    from app.social.poster import resembles_recent

    recent = [
        "Closed $DOGE at 0.07827 after 15 hours. +2.2% on price, +2.0% on margin. dorking...",
        "Out of $ENA at 0.21196. +2.2% on price, +2.0% on margin, 39 hours. still dorking around",
    ]
    assert resembles_recent(
        "Closed $XRP at 1.3368 after 41 hours. -2.1% on price, -2.3% on margin. dorking...", recent
    )
    assert (
        resembles_recent(
            "Forty-one hours of $XRP going the wrong way, slowly. -2.3% on margin, on paper,"
            " which is the only reason I can still joke about it.",
            recent,
        )
        is None
    )


def test_record_prices_pass_the_whitelist_in_any_context() -> None:
    from app.social.poster import known_numbers

    known = known_numbers(FACTS)
    assert check("$SOL: 74.10 was the entry, 77.60 the exit.", known=known).ok
    assert not check("$SOL: 74.10 was the entry, 99.99 something else.", known=known).ok


async def test_compose_rewrites_a_lookalike_and_skips_after_three(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    from types import SimpleNamespace

    from app.config import get_config
    from app.social import poster

    recent = ["Closed $DOGE at 0.07827 after 15 hours. +2.2% on price, +2.0% on margin. dorking..."]
    drafts = iter(
        [
            "Closed $SOL at 77.60 after 9 hours. +4.7% on price, +13.6% on margin. dorking...",
            "Nine hours in $SOL and the range finally gave way. +13.6% on margin, paper money,"
            " so the only thing compounding is my confidence.",
        ]
    )
    calls: list[str] = []

    async def fake_complete(task: str, schema: type, **kw: object) -> SimpleNamespace:
        calls.append(task)
        if task == "post_writer":
            return SimpleNamespace(parsed=poster.Draft(text=next(drafts)))
        return SimpleNamespace(parsed=poster.Audit(ok=True))

    monkeypatch.setattr(poster, "complete", fake_complete)
    monkeypatch.setattr(poster, "recent_posts", lambda session, n: recent)
    monkeypatch.setattr(poster, "repeats_archive", lambda session, cfg, text: None)
    text, source, ok, notes = await poster.compose(
        "close",
        FACTS,
        "plain",
        session=object(),
        cfg=get_config(),  # type: ignore[arg-type]
    )
    assert source == "writer" and ok and text and text.startswith("Nine hours")
    assert notes is not None and "same shape as a recent post" in notes
    assert calls.count("post_writer") == 2

    # every draft a lookalike: no post, no template
    lookalikes = iter(["Closed $SOL at 77.60 after 9 hours. +4.7% on price, +13.6% on margin."] * 3)
    monkeypatch.setattr(
        poster,
        "complete",
        lambda task, schema, **kw: _resolve(
            SimpleNamespace(parsed=poster.Draft(text=next(lookalikes)))
            if task == "post_writer"
            else SimpleNamespace(parsed=poster.Audit(ok=True))
        ),
    )
    text, source, ok, notes = await poster.compose(
        "close",
        FACTS,
        "plain",
        session=object(),
        cfg=get_config(),  # type: ignore[arg-type]
    )
    assert text is None and source == "skipped" and not ok and "no draft passed" in (notes or "")


async def _resolve(value: object) -> object:
    return value


async def test_the_writer_model_audits_when_the_auditor_is_down(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    from types import SimpleNamespace

    from app.config import get_config
    from app.llm import LLMError
    from app.social import poster

    models: list[object] = []

    async def fake_complete(task: str, schema: type, **kw: object) -> SimpleNamespace:
        if task == "post_writer":
            return SimpleNamespace(
                parsed=poster.Draft(
                    text="$SOL done, +13.6% on margin after nine hours. Paper, but I'll take the"
                    " practice."
                )
            )
        models.append(kw.get("model"))
        if kw.get("model") is None:
            raise LLMError("auditor out of credits")
        return SimpleNamespace(parsed=poster.Audit(ok=True))

    monkeypatch.setattr(poster, "complete", fake_complete)
    monkeypatch.setattr(poster, "recent_posts", lambda session, n: [])
    monkeypatch.setattr(poster, "repeats_archive", lambda session, cfg, text: None)
    cfg = get_config()
    text, source, ok, _ = await poster.compose(
        "close",
        FACTS,
        "plain",
        session=object(),
        cfg=cfg,  # type: ignore[arg-type]
    )
    assert ok and source == "writer" and text
    assert models == [None, cfg.models.post_writer]
