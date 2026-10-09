"""One-time admin setup on the server: username, password, TOTP enrolment QR.

python -m app.admin.setup --username lex          # prompts for the password, prints the QR
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import UTC, datetime

import qrcode

from app.admin import auth
from app.db.models import AdminUser
from app.db.session import new_session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", help="omit to be prompted")
    args = parser.parse_args(argv)
    password = args.password or getpass.getpass("admin password (12+ chars): ")
    if len(password) < 12:
        print("password too short")
        return 2
    secret = auth.new_totp_secret()
    with new_session() as s:
        user = s.get(AdminUser, 1)
        if user is None:
            user = AdminUser(
                id=1,
                username=args.username,
                password_hash=auth.hash_password(password),
                totp_secret=secret,
                created_at=datetime.now(UTC),
            )
            s.add(user)
        else:
            user.username, user.password_hash, user.totp_secret, user.totp_enrolled = (
                args.username,
                auth.hash_password(password),
                secret,
                False,
            )
        s.commit()
    uri = auth.totp_uri(secret, args.username)
    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    qr.print_ascii(invert=True)
    print("Scan with your authenticator, then confirm the first code:")
    code = input("code: ")
    with new_session() as s:
        user = s.get(AdminUser, 1)
        assert user is not None
        if auth.verify_totp(secret, code):
            user.totp_enrolled = True
            s.commit()
            print("TOTP enrolled. Log in at the admin domain.")
            return 0
    print("code did not match; run again")
    return 1


if __name__ == "__main__":
    sys.exit(main())
