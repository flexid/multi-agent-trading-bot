"""Ops jobs scheduled from the scheduler (M9): nightly backup, go-live check, tuning,
monthly cost report."""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from app.config import Config, get_secrets
from app.costs import SERVER_USD_PER_MONTH, X_POST_USD, X_READ_USD
from app.db.models import LLMCall, XPostOut, XPostRecord
from app.db.session import new_session

log = logging.getLogger("ops")
BACKUP_DIR = Path("/app/logs/backups")
KEEP_BACKUPS = 14


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

        push_r2({f"backups/{out.name}": (out.read_bytes(), "application/octet-stream")})
    except Exception as exc:
        log.warning("backup not pushed to R2: %s", exc)
    log.info("backup written: %s (%d bytes)", out, out.stat().st_size)
    return out


def golive_check(cfg: Config) -> None:
    from app.risk.golive import capital_ramp, decide, ramp

    with new_session() as s:
        verdict = decide(s, cfg)
        ramp(s)
        capital_ramp(s, cfg)
    log.info(
        "go-live check: %s (%d/%d criteria)",
        "READY" if verdict.ready else "not yet",
        sum(c.ok for c in verdict.criteria),
        len(verdict.criteria),
    )


def tuning(cfg: Config) -> None:
    from app.decision.tuning import tune

    with new_session() as s:
        new = tune(s, cfg)
    log.info("weight tuning: %s", new or "skipped (fewer than 100 closed trades)")


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
            .where(XPostOut.posted_at >= since, XPostOut.dry_run.is_(False))
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
