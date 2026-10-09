"""Admin auth (SPEC §12 M8b): single user, argon2 password + TOTP, lockout, sessions.

Sessions are signed cookies (itsdangerous) with a short lifetime; TOTP is re-asked for
the kill switch, resume and parameter changes ("step-up"). Rate limiting and lockout
live on the user row so they survive restarts.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.db.models import AdminUser, AuditLog

SESSION_TTL_S = 8 * 60 * 60  # one owner, one dashboard: a working day; step-up stays short
STEP_UP_TTL_S = 5 * 60
MAX_FAILED = 5
LOCKOUT = timedelta(minutes=15)
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except VerifyMismatchError:
        return False


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str, issuer: str = "dorkbot admin") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def audit(
    session: Session,
    actor: str,
    action: str,
    ip: str | None,
    detail: dict[str, object] | None = None,
    ok: bool = True,
) -> None:
    session.add(
        AuditLog(ts=datetime.now(UTC), actor=actor, ip=ip, action=action, detail=detail, ok=ok)
    )
    session.commit()


def login(
    session: Session, username: str, password: str, code: str, ip: str | None
) -> tuple[AdminUser | None, str]:
    """Returns (user, reason). Any failure counts toward lockout; reasons stay generic."""
    now = datetime.now(UTC)
    user = session.get(AdminUser, 1)
    if user is None or user.username != username:
        audit(session, username, "login", ip, {"reason": "unknown user"}, ok=False)
        return None, "invalid credentials"
    if user.locked_until and user.locked_until > now:
        audit(session, username, "login", ip, {"reason": "locked"}, ok=False)
        return None, "locked, try again later"
    ok = verify_password(user.password_hash, password) and (
        not user.totp_enrolled or verify_totp(user.totp_secret, code)
    )
    if not ok:
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED:
            user.locked_until, user.failed_logins = now + LOCKOUT, 0
        audit(session, username, "login", ip, {"reason": "bad password or code"}, ok=False)
        return None, "invalid credentials"
    user.failed_logins = 0
    new_ip = ip is not None and ip not in (user.known_ips or [])
    if new_ip:
        user.known_ips = [*(user.known_ips or []), ip][-20:]
    audit(session, username, "login", ip, {"new_ip": new_ip})
    return user, "new_ip" if new_ip else "ok"


class Sessions:
    def __init__(self, secret_key: str) -> None:
        self._s = URLSafeTimedSerializer(secret_key, salt="admin-session")

    def issue(self, username: str, *, step_up: bool = False) -> str:
        return self._s.dumps({"u": username, "n": secrets.token_hex(8), "su": step_up})

    def read(self, token: str | None, *, max_age: int = SESSION_TTL_S) -> dict[str, object] | None:
        if not token:
            return None
        try:
            data = self._s.loads(token, max_age=max_age)
        except BadSignature:
            return None
        return data if isinstance(data, dict) else None

    def csrf(self, token: str) -> str:
        return self._s.dumps({"csrf": token[-16:]})

    def csrf_ok(self, token: str, csrf: str | None) -> bool:
        data = self.read(csrf, max_age=SESSION_TTL_S) if csrf else None
        return bool(data and data.get("csrf") == token[-16:])
