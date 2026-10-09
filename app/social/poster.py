"""X poster (SPEC §11): post after a fill, never before; open = new post, close = reply.

Pipeline per event: facts from the trade record → writer (claude-sonnet-5-5) → auditor
(gpt-5.6-terra) → number whitelist → on any failure a template. The post is queued with
a random 1–10 minute delay and sent by ``flush`` from the executor loop. Above
``max_posts_per_day`` closes are bundled into one daily summary. Dry-run unless
``posting.enabled`` and (live mode or ``post_in_shadow``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import random
import time
import urllib.parse
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config, Secrets
from app.db.models import Position, XPostOut
from app.llm import LLMError, complete, load_prompt
from app.social import templates as tpl
from app.social.whitelist import check

log = logging.getLogger("poster")
MAX_LEN = 270


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=280)


class Audit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    violations: list[str] = Field(default_factory=list, max_length=10)


def facts_for(pos: Position, cfg: Config, reason: str | None, paper: bool) -> tpl.TradeFacts:
    hold = None
    if pos.opened_at and pos.closed_at:
        hold = tpl.holding_text((pos.closed_at - pos.opened_at).total_seconds() / 3600)
    return tpl.TradeFacts(
        cashtag=cfg.posting.cashtags.get(pos.asset, f"${pos.asset}"),
        direction=pos.direction,
        entry=pos.entry_price or Decimal(0),
        leverage=pos.leverage,
        stop=pos.stop,
        target=pos.target,
        exit=pos.exit_price,
        pnl_price_pct=pos.pnl_price_pct,
        pnl_margin_pct=pos.pnl_margin_pct,
        holding=hold,
        reason=reason,
        paper=paper,
    )


async def compose(
    kind: str, facts: tpl.TradeFacts, style: str, *, use_models: bool = True
) -> tuple[str, str, bool, str | None]:
    """Returns (text, source, audit_ok, notes). Falls back to a template on any failure.
    Dry-run posts (nobody sees them) are templates only: no model spend."""
    if not use_models:
        fallback = tpl.render_open(facts) if kind == "open" else tpl.render_close(facts)
        return fallback, "template", True, "dry-run: template only"
    record = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in facts.__dict__.items()}
    try:
        draft = await complete(
            "post_writer",
            Draft,
            prompt=load_prompt("post_writer"),
            user_text=f'{{"kind": "{kind}", "record": {record}, "style": {style!r}}}',
        )
        text = draft.parsed.text.strip()
        if facts.paper and not text.lower().startswith("paper"):
            text = "Paper: " + text
        audit = await complete(
            "post_auditor",
            Audit,
            prompt=load_prompt("post_auditor"),
            user_text=f'{{"draft": {text!r}, "record": {record}}}',
        )
        wl = check(text)
        ok = audit.parsed.ok and wl.ok and len(text) <= MAX_LEN
        if ok:
            return text, "writer", True, None
        notes = "; ".join(audit.parsed.violations + wl.problems) or "too long"
    except LLMError as exc:
        notes = f"writer/auditor failed: {exc}"[:300]
    fallback = tpl.render_open(facts) if kind == "open" else tpl.render_close(facts)
    return fallback, "template", False, notes


async def enqueue(
    session: Session,
    cfg: Config,
    pos: Position,
    kind: str,
    *,
    reason: str | None,
    style: str,
    dry_run: bool,
    now: datetime | None = None,
) -> XPostOut:
    now = now or datetime.now(UTC)
    paper = pos.mode == "paper"
    text, source, audit_ok, notes = await compose(
        kind, facts_for(pos, cfg, reason, paper), style, use_models=not dry_run
    )
    wl = check(text)
    lo, hi = cfg.posting.delay_minutes
    reply_to = None
    if kind == "close":
        opener = session.execute(
            select(XPostOut.x_id).where(XPostOut.position_id == pos.id, XPostOut.kind == "open")
        ).scalar_one_or_none()
        reply_to = opener
    row = XPostOut(
        position_id=pos.id,
        kind=kind,
        text=text,
        source=source,
        audit_ok=audit_ok,
        audit_notes=notes,
        whitelist_ok=wl.ok,
        scheduled_at=now + timedelta(minutes=random.uniform(lo, hi)),
        reply_to_x_id=reply_to,
        dry_run=dry_run,
        created_at=now,
    )
    session.add(row)
    session.commit()
    return row


def posts_today(session: Session, now: datetime) -> int:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        session.execute(
            select(func.count())
            .select_from(XPostOut)
            .where(XPostOut.posted_at >= day, XPostOut.dry_run.is_(False))
        ).scalar_one()
    )


# --- X API v2 write (OAuth 1.0a user context) --------------------------------------


def _oauth_header(secrets: Secrets, method: str, url: str) -> str:
    params = {
        "oauth_consumer_key": secrets.x_api_key.get_secret_value(),
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": secrets.x_access_token.get_secret_value(),
        "oauth_version": "1.0",
    }
    base = "&".join(
        urllib.parse.quote(p, safe="")
        for p in (
            method,
            url,
            "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in sorted(params.items())),
        )
    )
    key = (
        urllib.parse.quote(secrets.x_api_secret.get_secret_value(), safe="")
        + "&"
        + urllib.parse.quote(secrets.x_access_token_secret.get_secret_value(), safe="")
    )
    params["oauth_signature"] = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()
    return "OAuth " + ", ".join(
        f'{k}="{urllib.parse.quote(v, safe="")}"' for k, v in sorted(params.items())
    )


async def send(secrets: Secrets, text: str, reply_to: str | None) -> str:
    url = "https://api.x.com/2/tweets"
    body: dict[str, object] = {"text": text}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": reply_to}
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(
            url, json=body, headers={"Authorization": _oauth_header(secrets, "POST", url)}
        )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"X post failed: HTTP {r.status_code} {r.text[:200]}")
    return str(r.json()["data"]["id"])


async def flush(
    session: Session, cfg: Config, secrets: Secrets, now: datetime | None = None
) -> int:
    """Send due posts. Dry-run rows are marked posted without a request. Returns sent count."""
    now = now or datetime.now(UTC)
    due = session.scalars(
        select(XPostOut)
        .where(XPostOut.posted_at.is_(None), XPostOut.scheduled_at <= now)
        .order_by(XPostOut.id)
    ).all()
    sent = 0
    for row in due:
        if not row.whitelist_ok:
            row.error, row.posted_at = "blocked by whitelist", now  # never leaves the box
            continue
        if row.dry_run:
            row.posted_at, row.x_id = now, f"dry-{row.id}"
            continue
        if row.kind == "close" and posts_today(session, now) >= cfg.posting.max_posts_per_day:
            row.kind, row.error = "summary", "bundled: daily cap reached"
            row.scheduled_at = now.replace(hour=23, minute=55, second=0, microsecond=0)
            continue
        try:
            row.x_id = await send(secrets, row.text, row.reply_to_x_id)
            row.posted_at = now
            sent += 1
        except Exception as exc:
            row.error = str(exc)[:500]
            log.error("post %d failed: %s", row.id, exc)
    session.commit()
    return sent
