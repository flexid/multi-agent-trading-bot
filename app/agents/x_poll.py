"""The news tripwire (v10, owner 2026-10-12): every few minutes the curated X accounts are
read through the ticker-filtered search and every new post is labelled by Jev. A credible,
fresh shock on a traded asset starts a decision cycle at once instead of at the next
four-hour mark. The executor reads the same labels for its shock exit.

Reads are billed per post, so polling often costs no more than once a cycle; the since
markers make a quiet poll free. The poll never spends more than ``POLL_READS`` of the daily
budget in one go, so the cycles keep their share.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app import triggers
from app.agents import x_sentiment as xs
from app.config import Config, get_secrets
from app.data.x import XClient
from app.db.session import new_session
from app.llm import is_jev, load_prompt

log = logging.getLogger("x_poll")
POLL_READS = 40


async def poll(cfg: Config, now: datetime | None = None) -> str | None:
    """Fetch, label, and return the reason for a cycle when a shock warrants one."""
    now = now or datetime.now(UTC)
    model = cfg.models.catalysts if is_jev(cfg.models.catalysts) else None
    with new_session() as session:
        budget_left = cfg.budget.x_reads_per_day - xs.reads_today(session, now)
        allowance = min(POLL_READS, budget_left)
        if allowance < 10:
            return None
        async with XClient(get_secrets().x_bearer_token) as client:
            stored, _ = await xs.fetch_curated(session, cfg, client, now, allowance)
        if stored:
            await xs.label_unlabeled(
                session, cfg, load_prompt("x_sentiment"), None, force_model=model
            )
            log.info("x poll: %d new post(s) labelled", stored)
        reason = triggers.x_shock_trigger(session, now)
        if reason is None:
            return None
        return triggers.check(session, cfg, now)  # the gap and the daily cap still apply
