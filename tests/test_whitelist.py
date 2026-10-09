from app.social.whitelist import check


def test_spec_examples_pass() -> None:
    a = (
        "took a $SOL long at 74.10, 3x. range finally broke and polymarket odds on a green "
        "week went from 48 to 61%. stop 72.30, target 78"
    )
    b = (
        "closed $SOL at 77.60 after 9 hours. +4.7% on price, +13.6% on margin. "
        "ran out of steam just before target, i'll take it"
    )
    assert check(a).ok, check(a).problems
    assert check(b).ok, check(b).problems


def test_amounts_and_balances_fail() -> None:
    assert not check("took a $BTC long, 2000 usdt in").ok
    assert not check("balance now 10432.5").ok
    assert "number without allowed context: '0.0075'" in check("bought 0.0075 of it").problems


def test_links_and_advice_fail() -> None:
    assert "contains a link" in check("see dorkbot.dev for details").problems
    assert "advice language" in check("buy now before it will pump").problems


def test_leverage_percent_and_durations_pass() -> None:
    assert check("5x, +2.1% on price after 36h, stop at 100.5").ok
    assert check("flat for 3 days, -0.8% on margin").ok


def test_footer_allowed_on_site_only() -> None:
    footer = "nfa. just a bot trading its own bag."
    assert sorted(check(footer).problems) == ["forbidden word: 'bot'", "forbidden word: 'nfa'"]
    assert check(footer, site=True).ok
