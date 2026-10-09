"""Kill switch, pause and resume from the command line (the admin UI arrives in M8b).

python -m app.control kill  --reason "..."   # close all, repay, freeze
python -m app.control pause --reason "..."   # no new positions
python -m app.control resume                 # also clears the emergency brake
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from app.db.models import ControlRequest
from app.db.session import new_session


def request(kind: str, reason: str | None, source: str = "cli") -> int:
    with new_session() as session:
        req = ControlRequest(ts=datetime.now(UTC), kind=kind, source=source, reason=reason)
        session.add(req)
        session.commit()
        return req.id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["kill", "pause", "resume"])
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    rid = request(args.kind, args.reason)
    print(f"{args.kind} request #{rid} queued; the executor applies it within its next tick")
    return 0


if __name__ == "__main__":
    sys.exit(main())
