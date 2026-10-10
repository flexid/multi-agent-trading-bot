import re
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from app.admin import auth
from app.admin.app import EDITABLE
from app.admin.env import ADMIN_ENV_KEYS, FORBIDDEN_PREFIXES

ROOT = Path(__file__).resolve().parents[1]


def test_admin_container_gets_no_trading_secrets() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    admin = compose[compose.index("  admin:") : compose.index("  executor:")]
    assert "env_file: .env.admin" in admin and ".env\n" not in admin.replace(".env.admin", "")
    script = (ROOT / "scripts" / "admin_env.sh").read_text()
    allowed = set(re.search(r"\^\((.*?)\)=", script).group(1).split("|"))  # type: ignore[union-attr]
    assert allowed == set(ADMIN_ENV_KEYS)
    assert not any(k.startswith(FORBIDDEN_PREFIXES) for k in allowed)
    example = (ROOT / ".env.example").read_text()
    out = subprocess.run(
        ["grep", "-E", rf"^({'|'.join(ADMIN_ENV_KEYS)})=", "-"],
        input=example,
        text=True,
        capture_output=True,
    ).stdout
    assert "BYBIT" not in out and "X_API" not in out


def test_password_and_totp_roundtrip() -> None:
    h = auth.hash_password("correct horse battery staple")
    assert auth.verify_password(h, "correct horse battery staple")
    assert not auth.verify_password(h, "wrong")
    secret = auth.new_totp_secret()
    import pyotp

    assert auth.verify_totp(secret, pyotp.TOTP(secret).now())
    assert not auth.verify_totp(secret, "000000")
    assert "otpauth://totp/" in auth.totp_uri(secret, "lex")


def test_sessions_sign_and_expire() -> None:
    s = auth.Sessions("k" * 32)
    tok = s.issue("lex")
    assert s.read(tok) is not None and s.read(tok)["u"] == "lex"  # type: ignore[index]
    assert s.read(tok + "x") is None
    assert s.read(tok, max_age=-1) is None
    csrf = s.csrf(tok)
    assert s.csrf_ok(tok, csrf) and not s.csrf_ok(tok, "nope")
    assert not s.read(s.issue("lex"))["su"]  # type: ignore[index]
    assert s.read(s.issue("lex", step_up=True))["su"]  # type: ignore[index]


def test_editable_bounds_keep_leverage_at_or_below_ten() -> None:
    assert EDITABLE["trading.leverage_max"][1] == Decimal(10)
    assert "trading.leverage_max_spx6900" not in EDITABLE  # dropped with the asset (2026-10-10)
    for key, (lo, hi) in EDITABLE.items():
        assert lo < hi, key


def test_toml_value_replacement_keeps_comments() -> None:
    from app.execution.executor import _set_toml_value

    text = (
        "[trading]\nleverage_max = 10              # cap\nrisk_per_trade = 0.005\n"
        "[posting]\nmax_posts_per_day = 20\n"
    )
    new, n = _set_toml_value(text, "trading", "leverage_max", Decimal(5))
    assert n == 1 and "leverage_max = 5  # cap" in new and "max_posts_per_day = 20" in new
    _, n = _set_toml_value(text, "posting", "leverage_max", Decimal(5))
    assert n == 0


@pytest.mark.parametrize("prefix", FORBIDDEN_PREFIXES)
def test_forbidden_prefixes_are_not_in_admin_keys(prefix: str) -> None:
    assert not any(k.startswith(prefix) for k in ADMIN_ENV_KEYS)


def test_side_filter_colours_long_and_short_and_escapes_html() -> None:
    from app.admin.app import side_words

    out = str(side_words("Short <b>BTC</b>, go long; longer is not a side"))
    assert '<span class="short">Short</span>' in out
    assert '<span class="long">long</span>' in out
    assert "&lt;b&gt;" in out and "<b>" not in out
    assert "longer" in out and '<span class="long">longer' not in out
    assert str(side_words(None)) == ""


def test_stale_session_logs_out_with_a_notice_instead_of_403() -> None:
    from fastapi.testclient import TestClient

    from app.admin.app import COOKIE, app

    client = TestClient(app, follow_redirects=False)
    client.cookies.set(COOKIE, "stale-token")
    r = client.post("/logout", data={"csrf": "whatever"})
    assert r.status_code == 303 and r.headers["location"] == "/login?reason=expired"
    page = client.get("/login?reason=expired")
    assert page.status_code == 200 and "You were logged out" in page.text
    assert any(
        c.startswith(f"{COOKIE}=") and "Max-Age=0" in c for c in page.headers.get_list("set-cookie")
    )


def test_money_filter_formats_european_with_dollar() -> None:
    from decimal import Decimal

    from app.admin.app import fmt_money

    assert fmt_money(Decimal("10019.95")) == "$ 10.019,95"
    assert fmt_money(185) == "$ 185,00"
    assert fmt_money(Decimal("-15.7069")) == "-$ 15,71"
    assert fmt_money(Decimal("13.38"), signed=True) == "+$ 13,38"
    assert fmt_money(None) == "$ 0,00"
