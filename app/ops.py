"""Ops jobs scheduled from the scheduler (M9): nightly backup, go-live check, tuning,
monthly cost report, and the daily look at inputs that keep failing."""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config, get_secrets
from app.costs import SERVER_USD_PER_MONTH, X_POST_USD, X_READ_USD
from app.db.models import DataSource, LLMCall, XPostOut, XPostRecord
from app.db.session import new_session

log = logging.getLogger("ops")
BACKUP_DIR = Path("/app/logs/backups")
KEEP_BACKUPS = 14
# An input is "failing" when it has not succeeded for this long and is still being tried.
FAILING_AFTER = timedelta(hours=6)


def failing_sources(session: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """Inputs that keep failing: no success for six hours, errors in a row, and an error in
    the last day (so a source nobody asks for any more is not reported). The numbered
    queries of one source (x.curated.q0, q1, ...) are one line.

    2026-11-13: X had answered every read with "402 Payment Required" for a week and every
    post with 429 for five days; nothing told the owner."""
    now = now or datetime.now(UTC)
    rows = session.scalars(
        select(DataSource)
        .where(DataSource.consecutive_errors >= 3, DataSource.last_error_at >= now - timedelta(1))
        .order_by(DataSource.name)
    ).all()
    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.last_success_at is not None and r.last_success_at > now - FAILING_AFTER:
            continue
        name = re.sub(r"\.q\d+$", "", r.name)
        g = grouped.setdefault(
            name,
            {"source": name, "parts": 0, "since": r.last_success_at, "errors_in_a_row": 0},
        )
        g["parts"] += 1
        g["errors_in_a_row"] = max(g["errors_in_a_row"], r.consecutive_errors)
        g["last_error"] = (r.last_error or "")[:160]
        if r.last_success_at is not None and (g["since"] is None or r.last_success_at < g["since"]):
            g["since"] = r.last_success_at
    out = []
    for g in grouped.values():
        since = g["since"]
        g["since"] = since.strftime("%Y-%m-%d %H:%M") if since else None
        out.append(g)
    return out


def describe_failing(sources: list[dict[str, Any]]) -> list[str]:
    return [
        f"{s['source']}"
        + (f" ({s['parts']} queries)" if s["parts"] > 1 else "")
        + (f": no success since {s['since']} UTC" if s["since"] else ": never succeeded")
        + f", {s['errors_in_a_row']} errors in a row; last answer: {s['last_error'] or 'none'}"
        for s in sources
    ]


def sources_alert(now: datetime | None = None) -> list[dict[str, Any]]:
    """One mail a day for as long as an input keeps failing. Returns what was reported."""
    from app.admin import notify

    with new_session() as session:
        failing = failing_sources(session, now)
    if failing:
        lines = describe_failing(failing)
        notify.send(
            "dorkbot: an input has been failing for hours",
            "The bot keeps running, but it decides without:\n\n- "
            + "\n- ".join(lines)
            + "\n\nA 401, 402 or 403 from a paid API is usually the account (credit, plan, "
            "key), not the bot. This mail repeats daily until the source answers again.",
        )
        log.warning("failing inputs reported: %s", "; ".join(lines))
    return failing


def backup() -> Path | None:
    """pg_dump to logs/backups (mounted volume), keep 14; pushed to R2 when configured."""
    url = get_secrets().database_url.get_secret_value()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = BACKUP_DIR / f"bot-{datetime.now(UTC):%Y%m%d-%H%M}.dump"
    try:
        subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(out), url.replace("+psycopg", "")],
            check=True,
            timeout=600,
            capture_output=True,
        )
    except Exception as exc:
        log.error("backup failed: %s", exc)
        return None
    for old in sorted(BACKUP_DIR.glob("bot-*.dump"))[:-KEEP_BACKUPS]:
        old.unlink()
    try:
        from app.site.publish import push_r2

        private = get_secrets().r2_backup_bucket
        if private:  # never the public bucket
            push_r2({out.name: (out.read_bytes(), "application/octet-stream")}, bucket=private)
    except Exception as exc:
        log.warning("backup not pushed to R2: %s", exc)
    log.info("backup written: %s (%d bytes)", out, out.stat().st_size)
    return out


def golive_check(cfg: Config) -> None:
    from app.risk import wickouts
    from app.risk.golive import capital_ramp, decide, ramp

    with new_session() as s:
        verdict = decide(s, cfg)
        ramp(s)
        capital_ramp(s, cfg)
        n = wickouts.check(s)
    if n:
        log.info("wick-out check: %d stopped trade(s) classified", n)
    log.info(
        "go-live check: %s (%d/%d criteria)",
        "READY" if verdict.ready else "not yet",
        sum(c.ok for c in verdict.criteria),
        len(verdict.criteria),
    )


def tuning(cfg: Config) -> None:
    from app.decision.tuning import tune
    from app.risk import wickouts

    with new_session() as s:
        new = tune(s, cfg)
        buffer = wickouts.tune_buffer(s, cfg.trading.assets)
    log.info("weight tuning: %s", new or "skipped (fewer than 100 closed trades)")
    log.info("stop buffer after wick-out tuning: %s ATR", buffer)


def cost_report(now: datetime | None = None) -> str:
    """Running costs for the last 30 days: LLM per model, X reads and posts, server."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=30)
    with new_session() as s:
        by_model = s.execute(
            select(
                LLMCall.model,
                func.count(),
                func.sum(LLMCall.input_tokens),
                func.sum(LLMCall.output_tokens),
                func.sum(LLMCall.cost_usd),
            )
            .where(LLMCall.ts >= since)
            .group_by(LLMCall.model)
            .order_by(func.sum(LLMCall.cost_usd).desc())
        ).all()
        reads = s.execute(
            select(func.count()).select_from(XPostRecord).where(XPostRecord.fetched_at >= since)
        ).scalar_one()
        posts = s.execute(
            select(func.count())
            .select_from(XPostOut)
            .where(
                XPostOut.posted_at >= since,
                XPostOut.dry_run.is_(False),
                XPostOut.x_id.is_not(None),
            )
        ).scalar_one()
    llm_total = sum(Decimal(str(r[4] or 0)) for r in by_model)
    x_total = Decimal(reads) * X_READ_USD + Decimal(posts) * X_POST_USD
    lines = [
        f"# Running costs, 30 days to {now:%Y-%m-%d}",
        "",
        "| Item | Count | Cost |",
        "| --- | --- | --- |",
    ]
    for model, n, tin, tout, cost in by_model:
        tokens = f"{tin or 0:,} in / {tout or 0:,} out tokens"
        lines.append(f"| LLM {model} | {n} calls, {tokens} | ${Decimal(str(cost or 0)):.2f} |")
    lines += [
        f"| X reads | {reads} | ${Decimal(reads) * X_READ_USD:.2f} |",
        f"| X posts | {posts} | ${Decimal(posts) * X_POST_USD:.2f} |",
        f"| Server (DigitalOcean) | 1 droplet | ${SERVER_USD_PER_MONTH:.2f} |",
        f"| **Total** | | **${llm_total + x_total + SERVER_USD_PER_MONTH:.2f}** |",
        "",
        "OpenAI rows use the prices in `[llm.pricing]`; placeholders until the owner fills "
        "them in.",
    ]
    return "\n".join(lines)


def write_cost_report() -> Path:
    out = Path("logs") / "costs.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(cost_report())
    return out
