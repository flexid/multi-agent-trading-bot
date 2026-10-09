"""Verify the X write path once: post a marked test line, then delete it.

python -m app.social.test_post
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from app.config import get_secrets
from app.db.models import XPostOut
from app.db.session import new_session
from app.social.poster import delete, send
from app.social.templates import with_marker
from app.social.whitelist import check


async def main() -> int:
    text = with_marker("Testing the write path, nothing to see here.")
    assert check(text).ok, check(text).problems
    secrets = get_secrets()
    x_id = await send(secrets, text, None)
    print(f"posted {x_id}: {text!r}")
    with new_session() as s:
        now = datetime.now(UTC)
        s.add(
            XPostOut(
                position_id=None,
                kind="test",
                text=text,
                source="template",
                audit_ok=True,
                audit_notes="write-path test",
                whitelist_ok=True,
                scheduled_at=now,
                posted_at=now,
                x_id=x_id,
                dry_run=False,
                created_at=now,
            )
        )
        s.commit()
    await asyncio.sleep(5)
    await delete(secrets, x_id)
    with new_session() as s:
        row = s.query(XPostOut).filter_by(x_id=x_id).one()
        row.error = "deleted after write-path test"
        s.commit()
    print(f"deleted {x_id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
