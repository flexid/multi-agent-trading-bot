"""Kill switch, pause and resume from the command line (the admin UI arrives in M8b).

python -m app.control kill  --reason "..."   # close all, repay, freeze
python -m app.control pause --reason "..."   # no new positions
python -m app.control resume                 # also clears the emergency brake
python -m app.control hold --hours 48        # open trades may be held this long (only longer)
python -m app.control arena-round            # the arena: end the round now, new rulebooks
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
    parser.add_argument("kind", choices=["kill", "pause", "resume", "hold", "arena-round"])
    parser.add_argument("--reason")
    parser.add_argument("--hours", type=int)
    args = parser.parse_args(argv)
    if args.kind == "hold" and args.hours is None:
        parser.error("hold needs --hours")
    # for "hold" the reason field carries the number of hours
    kind = args.kind.replace("-", "_")
    rid = request(kind, str(args.hours) if kind == "hold" else args.reason)
    who = (
        "the arena applies it on its next minute"
        if kind == "arena_round"
        else "the executor applies it within its next tick"
    )
    print(f"{args.kind} request #{rid} queued; {who}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
