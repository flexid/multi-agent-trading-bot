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
    assert EDITABLE["trading.leverage_max_spx6900"][1] == Decimal(3)
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
