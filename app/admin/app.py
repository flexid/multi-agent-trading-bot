"""Private admin (SPEC §12 M8b): FastAPI, server-rendered, behind Cloudflare.

Runs in its own container with no Bybit key. It only reads the database and writes
control requests and parameter changes; the executor applies them within hard bounds.

    uvicorn app.admin.app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.admin import auth, notify
from app.agents.names import BY_ID
from app.agents.names import display as agent_display
from app.agents.polymarket import coverage as pm_coverage
from app.config import Config, get_config, get_secrets
from app.db.models import (
    AccountSnapshot,
    AdminUser,
    AuditLog,
    Candle,
    ControlRequest,
    Cycle,
    DataSource,
    DecisionRecord,
    EquitySnapshot,
    Heartbeat,
    LLMCall,
    PaperAccount,
    ParamChange,
    Position,
    RiskRuleHit,
    RiskState,
    SuppressedTrigger,
    XPostOut,
    XPostRecord,
)
from app.db.session import new_session

app = FastAPI(title="dorkbot admin", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(Path(__file__).with_name("static"))), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))


def fmt_price(value: Any) -> str:
    """Prices with two decimals and thousands separators (owner, 2026-10-09); below $1 at
    least four significant digits (owner, 2026-10-10: PEPE is 0.000008)."""
    if value is None or value == "":
        return "-"
    try:
        d = Decimal(str(value))
    except Exception:
        return str(value)
    if abs(d) >= 1 or d == 0:
        return f"{d.quantize(Decimal('0.01')):,.2f}"
    places = max(2, 3 - d.adjusted())  # adjusted() = exponent of the leading digit
    return f"{d:.{min(places, 12)}f}"


SIDE_WORD = re.compile(r"\b(long|short)\b", re.IGNORECASE)


def side_words(value: Any) -> Markup:
    """Colour the words "long" (green) and "short" (red) wherever they appear
    (owner, 2026-10-09). The text is escaped first, so it is safe on raw reasons."""
    text = escape("" if value is None else str(value))
    return Markup(
        SIDE_WORD.sub(lambda m: f'<span class="{m.group(1).lower()}">{m.group(1)}</span>', text)
    )


def fmt_money(value: Any, signed: bool = False) -> str:
    """Ledger amounts on the overview as "$ 10.019,95" (owner, 2026-10-09): dot for
    thousands, comma for decimals. Tables keep plain numbers."""
    try:
        q = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except Exception:
        return str(value)
    body = f"{abs(q):,.2f}".replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")
    sign = "-" if q < 0 else ("+" if signed and q > 0 else "")
    return f"{sign}$ {body}"


TICKERS = {"SPX6900": "SPX"}  # how assets are named in the UI (owner, 2026-10-09)


def asset_tick(value: Any) -> str:
    return TICKERS.get(str(value), str(value))


templates.env.filters["price"] = fmt_price
templates.env.filters["tick"] = asset_tick
templates.env.filters["agent"] = agent_display
STANCE_CLASS = {"bullish": "up", "bearish": "down"}


def stance_words(value: str) -> Markup:
    """bullish in green, bearish in red; the value itself is escaped."""
    return Markup(f'<span class="{STANCE_CLASS.get(value, "muted")}">{escape(value)}</span>')


templates.env.filters["side_stance"] = stance_words
templates.env.filters["crew"] = lambda agent_id: (
    BY_ID[agent_id].name if agent_id in BY_ID else agent_id
)
templates.env.filters["money"] = fmt_money
templates.env.filters["side"] = side_words
COOKIE = "dorkbot_admin"
STEP_UP = "dorkbot_stepup"
CSRF = "dorkbot_csrf"

# Parameters the owner may change from the admin, with the hard bounds the executor
# enforces again in code. Leverage never above 10 (SPEC §12 M8b).
EDITABLE: dict[str, tuple[Decimal, Decimal]] = {
    "trading.leverage_max": (
        Decimal(1),
        Decimal(10),
    ),  # owner 2026-10-09: Bybit spot margin tops at 10x
    "trading.capital_share_per_asset": (Decimal("0.05"), Decimal("0.5")),
    "trading.gross_exposure_max": (Decimal(1), Decimal(3)),
    "trading.net_beta_exposure_max": (Decimal("0.5"), Decimal(5)),  # beta-weighted, × equity
    "trading.day_loss_stop": (Decimal("-0.05"), Decimal("-0.005")),
    "trading.depth_cap": (Decimal("0.01"), Decimal("0.1")),
    "risk.chase_max_atr": (Decimal(0), Decimal(10)),  # 0 = off (v9)
    "aggressive.take_profit_trade_pct": (Decimal(0), Decimal("0.5")),  # on margin, net; 0 = off
    "aggressive.take_profit_giveback": (
        Decimal(0),
        Decimal("0.9"),
    ),  # share of the best return given back
    "budget.api_usd_per_month": (Decimal(50), Decimal(5000)),
    "posting.max_posts_per_day": (Decimal(1), Decimal(100)),
}


def _sessions() -> auth.Sessions:
    key = get_secrets().admin_secret_key.get_secret_value()
    if not key:
        raise RuntimeError("ADMIN_SECRET_KEY is empty")
    return auth.Sessions(key)


def _ip(request: Request) -> str | None:
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else None
    )


def _set_cookie(resp: Response, name: str, value: str, max_age: int, request: Request) -> None:
    # Secure except over an SSH tunnel to localhost (used before Cloudflare fronts the admin).
    local = request.url.hostname in ("127.0.0.1", "localhost")
    resp.set_cookie(
        name, value, max_age=max_age, httponly=True, secure=not local, samesite="strict", path="/"
    )


LOGOUT_NOTICES = {
    "expired": "You were logged out: the session expired. Sign in again.",
    "session": "You were logged out: the session was no longer valid. Sign in again.",
    "bye": "Logged out.",
}


def _to_login(reason: str) -> HTTPException:
    """Back to the login page with a notice; the login page clears the old cookies."""
    return HTTPException(status_code=303, headers={"Location": f"/login?reason={reason}"})


def current_user(request: Request) -> str:
    data = _sessions().read(request.cookies.get(COOKIE))
    if not data:
        raise _to_login("expired") if request.cookies.get(COOKIE) else _to_login("")
    return str(data["u"])


def stepped_up(request: Request) -> bool:
    data = _sessions().read(request.cookies.get(STEP_UP), max_age=auth.STEP_UP_TTL_S)
    return bool(data and data.get("su"))


def check_csrf(request: Request, csrf: str) -> None:
    """A stale session or a token from another session never shows a 403 to the owner:
    it logs out with a notice (owner, 2026-10-09)."""
    token = request.cookies.get(COOKIE) or ""
    if not _sessions().read(token):
        raise _to_login("expired")
    if not _sessions().csrf_ok(token, csrf):
        raise _to_login("session")


def next_cycle_at(now: datetime | None = None) -> datetime:
    """The next regular cycle: every ``cycle_hours`` at minute 2 (app/scheduler.py)."""
    now = now or datetime.now(UTC)
    step = max(1, get_config().trading.cycle_hours)
    base = now.replace(minute=2, second=0, microsecond=0)
    base = base.replace(hour=(base.hour // step) * step)
    while base <= now:
        base += timedelta(hours=step)
    return base


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    token = request.cookies.get(COOKIE) or ""
    csrf = _sessions().csrf(token) if token else ""
    return templates.TemplateResponse(
        request, name, {"csrf": csrf, "next_cycle_at": next_cycle_at(), **ctx}
    )


# --- login -------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, reason: str = "") -> HTMLResponse:
    resp = templates.TemplateResponse(
        request, "login.html", {"error": None, "notice": LOGOUT_NOTICES.get(reason)}
    )
    if reason or request.cookies.get(COOKIE):
        for name in (COOKIE, STEP_UP, CSRF):  # whatever was there is gone: start clean
            resp.delete_cookie(name, path="/")
    return resp


@app.post("/login")
def login_post(
    request: Request, username: str = Form(), password: str = Form(), code: str = Form("")
) -> Response:
    ip = _ip(request)
    with new_session() as s:
        user, reason = auth.login(s, username, password, code, ip)
        if user is None:
            return templates.TemplateResponse(
                request, "login.html", {"error": reason}, status_code=401
            )
        if reason == "new_ip":
            notify.send(
                "dorkbot admin: login from a new IP",
                f"{username} logged in from {ip} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
            )
    resp = RedirectResponse("/", status_code=303)
    _set_cookie(resp, COOKIE, _sessions().issue(username), auth.SESSION_TTL_S, request)
    return resp


@app.post("/logout")
def logout(request: Request, csrf: str = Form()) -> Response:
    check_csrf(request, csrf)
    resp = RedirectResponse("/login?reason=bye", status_code=303)
    resp.delete_cookie(COOKIE)
    resp.delete_cookie(STEP_UP)
    return resp


@app.post("/step-up")
def step_up(
    request: Request, code: str = Form(), csrf: str = Form(), next: str = Form("/")
) -> Response:
    """TOTP again before a dangerous action (kill, resume, parameter change)."""
    user = current_user(request)
    check_csrf(request, csrf)
    with new_session() as s:
        row = s.get(AdminUser, 1)
        ok = bool(row and auth.verify_totp(row.totp_secret, code))
        auth.audit(s, user, "step_up", _ip(request), ok=ok)
    if not ok:
        raise HTTPException(status_code=401, detail="bad code")
    resp = RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    _set_cookie(resp, STEP_UP, _sessions().issue(user, step_up=True), auth.STEP_UP_TTL_S, request)
    return resp


# --- pages -----------------------------------------------------------------------------


def djaf_panel(s: Session, cfg: Config, now: datetime, month: datetime) -> dict[str, Any]:
    """Jev's cost and latency this month, Jev-vs-Sonnet agreement on events and posts, and
    the IC of both labellers' readings (variant main = Jev, alt = Sonnet)."""
    from app.agents.catalysts import agreement
    from app.db.models import CatalystEvent, XPostLabel
    from app.decision.tuning import LOOKBACK_DAYS, score_agents

    jev = s.execute(
        select(
            func.count(),
            func.coalesce(func.sum(LLMCall.cost_usd), 0),
            func.coalesce(func.avg(LLMCall.latency_ms), 0),
            func.coalesce(func.sum(LLMCall.input_tokens), 0),
        ).where(LLMCall.ts >= month, LLMCall.provider == "typesafe")
    ).one()
    events = s.scalars(
        select(CatalystEvent)
        .where(CatalystEvent.classified_at.is_not(None))
        .order_by(CatalystEvent.event_at.desc())
        .limit(400)
    ).all()
    ev_agree = agreement(list(events))
    labels = s.execute(
        select(XPostLabel.post_id, XPostLabel.labeler, XPostLabel.asset, XPostLabel.stance).where(
            XPostLabel.labeled_at >= now - timedelta(days=30)
        )
    ).all()
    by_post: dict[str, dict[str, tuple[str | None, str | None]]] = {}
    for post_id, labeler, asset, stance in labels:
        by_post.setdefault(post_id, {})[labeler] = (asset, stance)
    pairs = [v for v in by_post.values() if "jev" in v and "sonnet" in v]
    post_agree = {
        "posts": len(pairs),
        "asset": round(sum(v["jev"][0] == v["sonnet"][0] for v in pairs) / len(pairs), 3)
        if pairs
        else None,
        "stance": round(sum(v["jev"][1] == v["sonnet"][1] for v in pairs) / len(pairs), 3)
        if pairs
        else None,
    }
    since = now - timedelta(days=LOOKBACK_DAYS)
    main = {r.name: r for r in score_agents(s, cfg, since)}
    alt = {r.name: r for r in score_agents(s, cfg, since, variant="alt")}
    ic_rows = [
        {
            "agent": agent,
            "jev_1d": main[agent].ic.get("1d") if agent in main else None,
            "jev_n": main[agent].sample if agent in main else 0,
            "sonnet_1d": alt[agent].ic.get("1d") if agent in alt else None,
            "sonnet_n": alt[agent].sample if agent in alt else 0,
        }
        for agent in ("catalysts", "x_sentiment")
    ]
    recent = [e for e in events if e.source != "unlocks" or e.event_at >= now - timedelta(days=3)][
        :12
    ]
    return {
        "calls": int(jev[0]),
        "usd": float(jev[1]),
        "latency_ms": int(jev[2]),
        "tokens": int(jev[3]),
        "events": ev_agree,
        "posts": post_agree,
        "ic": ic_rows,
        "recent": recent,
    }


# --- dashboard pages: each panel has a data builder; a page runs only its own ----------


def _p_money(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    real = s.execute(
        select(AccountSnapshot).order_by(AccountSnapshot.ts.desc()).limit(1)
    ).scalar_one_or_none()
    real_coins = [
        c
        for c in (real.coins if real else [])
        if Decimal(str(c.get("wallet_balance") or 0)) != 0
        or Decimal(str(c.get("borrow_amount") or 0)) != 0
    ]
    return {"real": real, "real_coins": real_coins, **_p_ledgers(s, cfg, now, risk)}


def _p_ledgers(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    return {"ledgers": s.scalars(select(PaperAccount).order_by(PaperAccount.id)).all()}


def _p_sleeves(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    from app.risk.exposure import betas
    from app.risk.sleeves import lock_info, sleeve_account

    def lock_label(locks_: dict[str, Any], name_: str, now_: datetime) -> str:
        info = lock_info(locks_, name_, now_)
        if info is None or (risk is not None and risk.aggressive):
            return ""  # aggressive mode ignores every lock
        return (
            "all new trades locked"
            if len(info["dirs"]) == 2
            else f"new {next(iter(info['dirs']))}s locked"
        )

    beta = betas(s, cfg, now)
    rows = []
    locks = (risk.day_locked_sleeves or {}) if risk else {}
    modes = (risk.sleeve_modes or {}) if risk else {}
    for name in cfg.sleeves:
        rows.append(
            {
                "name": name,
                "cfg": cfg.sleeves[name],
                "acct": sleeve_account(s, cfg, name, "primary", now, beta, 1),
                "locked": lock_label(locks, name, now),
                "mode": modes.get(name, "shadow"),
            }
        )
    return {"sleeve_rows": rows, "beta": beta}


def _p_betas(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    from app.risk.exposure import beta_table

    return {"beta_rows": beta_table(s, cfg, now)}


def _p_open(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    rows = s.scalars(
        select(Position)
        .where(Position.status.in_(["open", "closing"]))
        .order_by(Position.opened_at)
    ).all()
    from app.site.snapshot import spot_for

    live: dict[int, dict[str, Any]] = {}
    for p in rows:
        spot = spot_for(s, p.symbol, now)
        if not spot or not p.entry_price:
            continue
        move = (spot / float(p.entry_price) - 1) * (1 if p.direction == "long" else -1)
        live[p.id] = {
            "price": spot,
            "price_pct": move * 100,
            "margin_pct": move * float(p.leverage) * 100,
            "usdt": move * float(p.notional),
        }
    return {"open_pos": rows, "open_live": live}


def _p_closed(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    rows = s.scalars(
        select(Position)
        .where(Position.status == "closed")
        .order_by(Position.closed_at.desc())
        .limit(30)
    ).all()
    return {"recent": rows}


def _p_costs(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    from app.costs_sleeves import costs_by_sleeve

    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    llm_day = s.execute(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(LLMCall.ts >= day)
    ).scalar_one()
    llm_month = s.execute(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(LLMCall.ts >= month)
    ).scalar_one()
    by_model = s.execute(
        select(LLMCall.model, func.count(), func.sum(LLMCall.cost_usd))
        .where(LLMCall.ts >= month)
        .group_by(LLMCall.model)
        .order_by(func.sum(LLMCall.cost_usd).desc())
    ).all()
    reads_month = s.execute(
        select(func.count()).select_from(XPostRecord).where(XPostRecord.fetched_at >= month)
    ).scalar_one()
    posts_month = s.execute(
        select(func.count())
        .select_from(XPostOut)
        .where(XPostOut.posted_at >= month, XPostOut.dry_run.is_(False))
    ).scalar_one()
    # Average per day over the last seven days (owner 2026-10-14): LLM, X, and the Bybit
    # fees the real pilot paid and the paper book would have paid.
    week = now - timedelta(days=7)
    first = s.execute(select(func.min(LLMCall.ts))).scalar_one()
    days = Decimal(
        str(max(1.0, min(7.0, ((now - first).total_seconds() / 86400) if first else 1.0)))
    )
    llm_week = Decimal(
        str(
            s.execute(
                select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(LLMCall.ts >= week)
            ).scalar_one()
        )
    )
    reads_week = s.execute(
        select(func.count()).select_from(XPostRecord).where(XPostRecord.fetched_at >= week)
    ).scalar_one()
    posts_week = s.execute(
        select(func.count())
        .select_from(XPostOut)
        .where(XPostOut.posted_at >= week, XPostOut.dry_run.is_(False))
    ).scalar_one()
    x_week = Decimal(reads_week) * Decimal("0.005") + Decimal(posts_week) * Decimal("0.015")

    def fees(track: str) -> Decimal:
        return Decimal(
            str(
                s.execute(
                    select(func.coalesce(func.sum(Position.fees), 0)).where(
                        Position.track == track, Position.opened_at >= week
                    )
                ).scalar_one()
            )
        )

    fees_pilot, fees_paper = fees("pilot"), fees("primary")
    daily: dict[str, Any] = {
        "days": float(days),
        "llm": llm_week / days,
        "x": x_week / days,
        "fees_pilot": fees_pilot / days,
        "fees_paper": fees_paper / days,
    }
    daily["total"] = (llm_week + x_week + fees_pilot) / days
    return {
        "daily": daily,
        "llm_day": llm_day,
        "llm_month": llm_month,
        "by_model": by_model,
        "reads_month": reads_month,
        "posts_month": posts_month,
        "x_month": Decimal(reads_month) * Decimal("0.005")
        + Decimal(posts_month) * Decimal("0.015"),
        "budget": cfg.budget.api_usd_per_month,
        "sleeve_costs": costs_by_sleeve(s, cfg, month),
    }


def _p_djaf(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {"djaf": djaf_panel(s, cfg, now, month)}


def _p_optima(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    from app.agents.optima import latest

    return {"optima": latest(s, max_age=timedelta(days=30))}


READING_AGENTS = ("indicators", "chart_patterns", "polymarket", "macro", "x_sentiment", "catalysts")


def _p_matrix(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    """The latest finished cycle as one table per sleeve: every reading's score and
    confidence per asset, the formula and consensus scores, and what each manager said."""
    from app.db.models import AgentOutputRecord, Cycle, DecisionRecord

    cycle = s.scalars(
        select(Cycle).where(Cycle.status == "done").order_by(Cycle.id.desc()).limit(1)
    ).first()
    if cycle is None:
        return {"matrix": None}
    outs = s.scalars(
        select(AgentOutputRecord).where(
            AgentOutputRecord.cycle_id == cycle.id, AgentOutputRecord.variant == "main"
        )
    ).all()
    decs = {
        d.asset: d
        for d in s.scalars(select(DecisionRecord).where(DecisionRecord.cycle_id == cycle.id)).all()
    }
    by_asset: dict[str, dict[str, Any]] = {}
    for o in outs:
        by_asset.setdefault(o.asset, {})[o.agent] = o
    groups = []
    sleeves = cfg.sleeves or {}
    names = list(sleeves) or ["all"]
    for name in names:
        assets = sleeves[name].assets if sleeves else cfg.trading.assets
        rows: list[dict[str, Any]] = [
            {"asset": a, "cells": by_asset.get(a, {}), "dec": decs.get(a)} for a in assets
        ]
        scores: list[float] = [
            float(d.formula_score)
            for d in (decs.get(a) for a in assets)
            if d is not None and d.formula_score is not None
        ]
        groups.append(
            {
                "name": name,
                "rows": rows,
                "agents": [ag for ag in READING_AGENTS if any(ag in r["cells"] for r in rows)],
                "mean_formula": sum(scores) / len(scores) if scores else None,
            }
        )
    return {"matrix": {"cycle": cycle, "groups": groups}}


def _p_sentiment(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    """Market mood: Fear & Greed, BTC dominance, the macro regime, and per asset the X
    sentiment of the last 24 hours (labelled posts by stance) with the mention volume."""
    from app.agents.x_sentiment import WINDOW_H, mention_volume
    from app.agents.x_sentiment import fresh_only as x_fresh_only
    from app.db.models import AgentOutputRecord, MacroObservation

    def series(name: str, days: int) -> tuple[float | None, float | None]:
        rows = s.execute(
            select(MacroObservation.date, MacroObservation.value)
            .where(
                MacroObservation.series == name,
                MacroObservation.date >= now - timedelta(days=days + 2),
            )
            .order_by(MacroObservation.date)
        ).all()
        if not rows:
            return None, None
        last = float(rows[-1][1])
        before = [float(v) for d, v in rows if d <= rows[-1][0] - timedelta(days=days)]
        return last, (last - before[-1]) if before else None

    fng, fng_d7 = series("FNG", 7)
    dom, dom_d7 = series("BTC_DOMINANCE", 7)
    macro = s.scalars(
        select(AgentOutputRecord)
        .where(AgentOutputRecord.agent == "macro", AgentOutputRecord.variant == "main")
        .order_by(AgentOutputRecord.id.desc())
        .limit(1)
    ).first()
    regime = None
    if macro and macro.evidence:
        parts = str(macro.evidence[0]).split(" ")
        regime = parts[1] if len(parts) > 1 else None
    since = now - timedelta(hours=WINDOW_H)
    stances = s.execute(
        select(XPostRecord.asset, XPostRecord.stance, func.count())
        .where(XPostRecord.labeled_at.is_not(None), XPostRecord.created_at >= since)
        .group_by(XPostRecord.asset, XPostRecord.stance)
    ).all()
    counts: dict[str, dict[str, int]] = {}
    for asset, stance, n in stances:
        if asset:
            counts.setdefault(asset, {})[stance or "neutral"] = int(n)
    recaps: dict[str, dict[str, int]] = {}  # asset -> stance -> posts labelled as recaps
    for asset, stance, n in s.execute(
        select(XPostRecord.asset, XPostRecord.stance, func.count())
        .where(
            XPostRecord.labeled_at.is_not(None),
            XPostRecord.created_at >= since,
            XPostRecord.fresh.is_(False),
        )
        .group_by(XPostRecord.asset, XPostRecord.stance)
    ).all():
        if asset:
            recaps.setdefault(asset, {})[stance or "neutral"] = int(n)
    judged = {
        a_: int(n)
        for a_, n in s.execute(
            select(XPostRecord.asset, func.count())
            .where(
                XPostRecord.labeled_at.is_not(None),
                XPostRecord.created_at >= since,
                XPostRecord.fresh.is_not(None),
            )
            .group_by(XPostRecord.asset)
        ).all()
        if a_
    }
    latest_x = {
        o.asset: o
        for o in s.scalars(
            select(AgentOutputRecord)
            .where(
                AgentOutputRecord.agent == "x_sentiment",
                AgentOutputRecord.variant == "main",
                AgentOutputRecord.computed_at >= now - timedelta(hours=12),
            )
            .order_by(AgentOutputRecord.id)
        ).all()
    }
    rows: list[dict[str, Any]] = []
    for asset in cfg.trading.assets:
        c = counts.get(asset, {})
        mv = mention_volume(s, asset, now)
        o = latest_x.get(asset)
        rows.append(
            {
                "asset": asset,
                "sleeve": cfg.sleeve_of(asset),
                "score": o.score if o else None,
                "confidence": o.confidence if o else None,
                "bullish": c.get("bullish", 0),
                "bearish": c.get("bearish", 0),
                "neutral": c.get("neutral", 0),
                "mentions": mv[0] if mv else None,
                "buzz": (mv[0] / mv[1]) if mv and mv[1] > 0 else None,
                "flags": list(o.risk_flags or []) if o else [],
                "judged": judged.get(asset, 0),
                "recap_bullish": recaps.get(asset, {}).get("bullish", 0),
                "recap_bearish": recaps.get(asset, {}).get("bearish", 0),
                "fresh_score": (
                    x_fresh_only(s, asset, 0, now).score if judged.get(asset, 0) else None
                ),
            }
        )
    scored: list[float] = [float(r["score"]) for r in rows if r["score"] is not None]
    return {
        "sentiment": {
            "fng": fng,
            "fng_d7": fng_d7,
            "dominance": dom,
            "dominance_d7": dom_d7,
            "regime": regime,
            "x_mean": sum(scored) / len(scored) if scored else None,
            "rows": rows,
            "window_h": WINDOW_H,
        }
    }


MACRO_SOURCES: dict[str, tuple[str, str]] = {
    "DGS2": ("2y treasury yield", "FRED"),
    "DGS10": ("10y treasury yield", "FRED"),
    "DTWEXBGS": ("broad dollar index", "FRED"),
    "VIXCLS": ("VIX", "FRED"),
    "SP500": ("S&P 500", "FRED"),
    "NASDAQCOM": ("Nasdaq composite", "FRED"),
    "XAUUSD": ("gold", "Bybit"),
    "STABLES_USD": ("stablecoin supply", "DefiLlama"),
    "FNG": ("Fear & Greed index", "alternative.me"),
    "BTC_DOMINANCE": ("BTC dominance", "CoinGecko"),
}


def _p_xposts(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    """The labelled posts behind Ears, all assets, last 24 hours: the most credible first.
    The text is untrusted and shown escaped, for the owner only."""
    from app.agents.x_sentiment import WINDOW_H

    rows = s.scalars(
        select(XPostRecord)
        .where(
            XPostRecord.labeled_at.is_not(None),
            XPostRecord.asset.is_not(None),
            XPostRecord.created_at >= now - timedelta(hours=WINDOW_H),
        )
        .order_by(XPostRecord.credibility.desc().nulls_last(), XPostRecord.created_at.desc())
        .limit(400)
    ).all()
    per_asset: dict[str, list[XPostRecord]] = {}
    for r in rows:
        bucket = per_asset.setdefault(r.asset or "", [])
        if len(bucket) < 8:  # the eight most credible per asset
            bucket.append(r)
    ordered = [(a, per_asset[a]) for a in cfg.trading.assets if a in per_asset]
    return {"xposts": ordered, "xposts_window_h": WINDOW_H, "xposts_total": len(rows)}


def _p_pmarkets(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    """The prediction markets behind Orak: per asset the busiest markets that passed every
    filter, with what they say and a link."""
    from app.agents.polymarket import Coverage, _points
    from app.db.models import PolymarketMarket

    out = []
    for asset in cfg.trading.assets:
        ladder, updown = _points(s, asset, now, Coverage(asset))
        if not ladder and not updown:
            continue
        ids = {p.market_id for p in ladder} | {u.market_id for u in updown}
        by_id = {
            m.id: m
            for m in s.scalars(select(PolymarketMarket).where(PolymarketMarket.id.in_(ids))).all()
        }
        rows: list[dict[str, Any]] = []
        for p in ladder:
            rows.append(
                {
                    "m": by_id.get(p.market_id),
                    "kind": f"{p.horizon} level",
                    "says": f"P(≥ {p.threshold:,.6g}) = {p.p_above:.0%}",
                    "volume": p.volume_24h,
                    "hours": p.hours_to_resolution,
                }
            )
        for u in updown:
            rows.append(
                {
                    "m": by_id.get(u.market_id),
                    "kind": f"up/down {u.window_min} min",
                    "says": f"P(up) = {u.p_up:.0%}",
                    "volume": u.volume_24h,
                    "hours": u.hours_to_resolution,
                }
            )
        rows.sort(key=lambda r: -float(r["volume"]))
        out.append({"asset": asset, "used": len(rows), "rows": rows[:6]})
    return {"pmarkets": out}


def _p_macro_inputs(
    s: Session, cfg: Config, now: datetime, risk: RiskState | None
) -> dict[str, Any]:
    """The series behind Storm: latest value, its date and where it comes from."""
    from app.db.models import MacroObservation

    rows = []
    for series, (label, source) in MACRO_SOURCES.items():
        last = s.execute(
            select(MacroObservation.date, MacroObservation.value, MacroObservation.fetched_at)
            .where(MacroObservation.series == series)
            .order_by(MacroObservation.date.desc())
            .limit(1)
        ).first()
        rows.append({"series": series, "label": label, "source": source, "last": last})
    candles = s.execute(
        select(func.count(), func.max(Candle.open_time)).where(Candle.interval == "15")
    ).one()
    return {"macro_inputs": rows, "candles_15m": candles}


def sparkline(values: list[float | None], top: float = 100.0, w: int = 240, h: int = 36) -> str:
    """SVG polyline points for a small chart: x across ``w``, y scaled so ``top`` is the
    ceiling. Gaps are skipped."""
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2 or top <= 0:
        return ""
    n = len(values) - 1
    return " ".join(f"{i / n * w:.1f},{h - min(1.0, max(0.0, v / top)) * h:.1f}" for i, v in pts)


def _p_server(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    """The droplet's usage from DigitalOcean monitoring (stored by the scheduler): the
    latest sample, the last hour's average, the 24-hour peak and a chart per measure."""
    from app.db.models import ServerMetric

    rows = s.scalars(
        select(ServerMetric)
        .where(ServerMetric.ts >= now - timedelta(hours=24))
        .order_by(ServerMetric.ts)
    ).all()
    if not rows:
        return {"server": None}
    last = rows[-1]
    info = next((r.info for r in reversed(rows) if r.info), None) or {}
    hour = [r for r in rows if r.ts >= now - timedelta(hours=1)]

    def stat(name: str) -> dict[str, Any]:
        day = [getattr(r, name) for r in rows if getattr(r, name) is not None]
        recent = [getattr(r, name) for r in hour if getattr(r, name) is not None]
        step = max(1, len(rows) // 160)
        series = [getattr(r, name) for r in rows[::step]]
        return {
            "now": getattr(last, name),
            "avg_1h": sum(recent) / len(recent) if recent else None,
            "peak_24h": max(day) if day else None,
            "series": series,
        }

    cpu, mem, disk = stat("cpu_pct"), stat("mem_used_pct"), stat("disk_used_pct")
    load = stat("load_1")
    vcpus = float(info.get("vcpus") or 1)
    gb = 1024**3
    return {
        "server": {
            "last": last,
            "age_min": int((now - last.ts).total_seconds() // 60),
            "info": info,
            "cpu": {**cpu, "spark": sparkline(cpu["series"])},
            "mem": {
                **mem,
                "spark": sparkline(mem["series"]),
                "total_gb": (last.mem_total or 0) / gb,
                "used_gb": (last.mem_total or 0) / gb * (last.mem_used_pct or 0) / 100,
            },
            "disk": {
                **disk,
                "spark": sparkline(disk["series"]),
                "total_gb": (last.disk_total or 0) / gb,
                "free_gb": (last.disk_total or 0) / gb * (1 - (last.disk_used_pct or 0) / 100),
            },
            "load": {**load, "spark": sparkline(load["series"], top=max(vcpus * 2, 1.0))},
            "vcpus": vcpus,
            "samples": len(rows),
        }
    }


def _p_charts_first(
    s: Session, cfg: Config, now: datetime, risk: RiskState | None
) -> dict[str, Any]:
    from app.decision.charts_first import CALL, compare

    return {"charts_first": compare(s, cfg, now - timedelta(days=90)), "charts_first_call": CALL}


def _p_solo(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    from app.decision.solo_calls import compare

    return {"solo": compare(s, cfg, now - timedelta(days=90))}


def _p_three(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    from app.decision.three_seat import compare

    return {"three": compare(s, cfg, now - timedelta(days=90))}


def _p_exposure(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    from app.risk.exposure import betas, track_exposure

    beta = betas(s, cfg, now)
    equity_of = {a.track: a.equity for a in s.scalars(select(PaperAccount)).all()}
    exposures = []
    for tr in ("primary", "max", "inverse", "pilot", "live"):
        if tr not in equity_of:
            continue
        e = track_exposure(s, tr, beta)
        eq = equity_of[tr]
        exposures.append((e, e.net_beta / eq if eq else None, e.gross / eq if eq else None))
    return {"beta": beta, "exposures": exposures}


def _p_stops(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    from app.risk.wickouts import stats as wick_stats

    return {"wicks": wick_stats(s, cfg.trading.assets)}


def _p_polymarket(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    return {"pm_cov": [pm_coverage(s, a, now) for a in cfg.trading.assets]}


def _p_freshness(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    return {"sources": s.scalars(select(DataSource).order_by(DataSource.name)).all()}


def _p_controls(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    return {}


def _p_arena(s: Session, cfg: Config, now: datetime, risk: RiskState | None) -> dict[str, Any]:
    """The arena (app/arena): round, traders, rulebooks, the latest decisions and trades,
    bench and round events, and what the two brains cost. Read from the ledger only."""
    from app.arena import ledger
    from app.db.models import (
        ArenaBenchEvent,
        ArenaDecision,
        ArenaEquity,
        ArenaRound,
        ArenaTrade,
        ArenaTrader,
    )

    a = cfg.arena
    rnd = ledger.current_round(s)
    traders = []
    for t in s.scalars(select(ArenaTrader).order_by(ArenaTrader.created_at)).all():
        eq = s.execute(
            select(ArenaEquity.equity_usdt, ArenaEquity.equity_index, ArenaEquity.ts)
            .where(ArenaEquity.trader == t.id)
            .order_by(ArenaEquity.ts.desc())
            .limit(1)
        ).first()
        rb = ledger.latest_rulebook(s, t.id, rnd.number) if rnd else None
        traders.append(
            {
                "id": t.id,
                "name": t.display_name,
                "model": t.model,
                "start_equity": t.start_equity,
                "equity": eq[0] if eq else None,
                "index": eq[1] if eq else None,
                "equity_at": eq[2] if eq else None,
                "losses": ledger.losses_in_round(s, t.id, rnd.number) if rnd else 0,
                "benched": (ledger.benched_in_round(s, t.id, rnd.number) is not None)
                if rnd
                else False,
                "open": ledger.open_trade(s, t.id),
                "rulebook": rb,
            }
        )
    decisions = s.scalars(select(ArenaDecision).order_by(ArenaDecision.id.desc()).limit(40)).all()
    acted = s.execute(
        select(func.count()).select_from(ArenaDecision).where(ArenaDecision.acted.is_(True))
    ).scalar_one()
    n_decisions = s.execute(select(func.count()).select_from(ArenaDecision)).scalar_one()
    trades = s.scalars(select(ArenaTrade).order_by(ArenaTrade.id.desc()).limit(30)).all()
    rounds = s.scalars(select(ArenaRound).order_by(ArenaRound.number.desc()).limit(20)).all()
    benches = s.scalars(select(ArenaBenchEvent).order_by(ArenaBenchEvent.at.desc()).limit(20)).all()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    costs = []
    for task in ("arena_author", "arena_reflex"):
        for label, since in (("today", day), ("all time", None)):
            q = select(
                func.count(),
                func.coalesce(func.sum(LLMCall.cost_usd), 0),
                func.avg(LLMCall.latency_ms),
                func.avg(LLMCall.input_tokens),
                func.sum(case((LLMCall.error.is_not(None), 1), else_=0)),
            ).where(LLMCall.task == task)
            if since is not None:
                q = q.where(LLMCall.ts >= since)
            n, cost, lat, tok, errs = s.execute(q).one()
            costs.append(
                {
                    "task": task,
                    "window": label,
                    "calls": n,
                    "cost": Decimal(str(cost)),
                    "latency_s": (lat or 0) / 1000,
                    "tokens": int(tok or 0),
                    "errors": int(errs or 0),
                }
            )
    failures = s.scalars(
        select(LLMCall)
        .where(LLMCall.task.in_(["arena_author", "arena_reflex"]), LLMCall.error.is_not(None))
        .order_by(LLMCall.ts.desc())
        .limit(10)
    ).all()
    return {
        "arena": {
            "enabled": a.enabled,
            "live": a.live,
            "market": a.market,
            "jev": a.jev_model,
            "round": rnd,
            "traders": traders,
            "decisions": decisions,
            "acted": acted,
            "n_decisions": n_decisions,
            "trades": trades,
            "rounds": rounds,
            "benches": benches,
            "costs": costs,
            "failures": failures,
            "losses_to_bench": a.losses_to_bench,
        }
    }


PANELS: dict[str, Any] = {
    "arena": _p_arena,
    "money": _p_money,
    "sleeves": _p_sleeves,
    "betas": _p_betas,
    "ledgers": _p_ledgers,
    "open": _p_open,
    "closed": _p_closed,
    "costs": _p_costs,
    "djaf": _p_djaf,
    "optima": _p_optima,
    "exposure": _p_exposure,
    "stops": _p_stops,
    "polymarket": _p_polymarket,
    "freshness": _p_freshness,
    "controls": _p_controls,
    "matrix": _p_matrix,
    "sentiment": _p_sentiment,
    "xposts": _p_xposts,
    "pmarkets": _p_pmarkets,
    "macro_inputs": _p_macro_inputs,
    "server": _p_server,
    "charts_first": _p_charts_first,
    "solo": _p_solo,
    "three": _p_three,
}
# The pages of the dashboard and the panels each one shows (the templates include the same).
PAGES: dict[str, tuple[str, ...]] = {
    "overview": ("money", "sleeves", "ledgers", "open", "controls"),
    "trades": ("open", "closed", "stops"),
    "risk": ("sleeves", "exposure", "betas", "stops"),
    "readings": ("matrix", "sentiment", "charts_first", "solo", "three"),
    "sources": ("xposts", "djaf", "pmarkets", "polymarket", "macro_inputs"),
    "costs": ("costs", "optima"),
    "system": ("server", "freshness", "controls"),
    "arena": ("arena",),
}


def dashboard(request: Request, user: str, page: str) -> HTMLResponse:
    now = datetime.now(UTC)
    cfg = get_config()
    ctx: dict[str, Any] = {}
    with new_session() as s:
        risk = s.get(RiskState, 1)
        heartbeats = s.scalars(select(Heartbeat)).all()
        pending_ctl = s.execute(
            select(func.count())
            .select_from(ControlRequest)
            .where(ControlRequest.applied_at.is_(None))
        ).scalar_one()
        for panel in PAGES[page]:
            ctx.update(PANELS[panel](s, cfg, now, risk))
    return render(
        request,
        f"{page}.html",
        user=user,
        now=now,
        cfg=cfg,
        risk=risk,
        heartbeats=heartbeats,
        stale={h.process: (now - h.ts) > timedelta(minutes=5) for h in heartbeats},
        pending_ctl=pending_ctl,
        stepped=stepped_up(request),
        **ctx,
    )


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    return dashboard(request, user, "overview")


@app.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    return dashboard(request, user, "trades")


@app.get("/risk", response_class=HTMLResponse)
def risk_page(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    return dashboard(request, user, "risk")


@app.get("/readings", response_class=HTMLResponse)
def readings_page(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    return dashboard(request, user, "readings")


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    return dashboard(request, user, "sources")


@app.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    return dashboard(request, user, "costs")


@app.get("/system", response_class=HTMLResponse)
def system_page(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    return dashboard(request, user, "system")


@app.get("/arena", response_class=HTMLResponse)
def arena_page(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    return dashboard(request, user, "arena")


@app.get("/decisions", response_class=HTMLResponse)
def decisions(
    request: Request,
    user: str = Depends(current_user),
    cycle: int | None = None,
    decision: int | None = None,
) -> HTMLResponse:
    with new_session() as s:
        cycles = s.scalars(select(Cycle).order_by(Cycle.id.desc()).limit(120)).all()
        if decision and not cycle:  # a link from a position: open that decision's cycle
            target = s.get(DecisionRecord, decision)
            cycle = target.cycle_id if target else None
        chosen = s.get(Cycle, cycle) if cycle else (cycles[0] if cycles else None)
        suppressed = s.scalars(
            select(SuppressedTrigger).order_by(SuppressedTrigger.id.desc()).limit(30)
        ).all()
        decs = (
            s.scalars(select(DecisionRecord).where(DecisionRecord.cycle_id == chosen.id)).all()
            if chosen
            else []
        )
        hits = (
            s.scalars(select(RiskRuleHit).where(RiskRuleHit.cycle_id == chosen.id)).all()
            if chosen
            else []
        )
        from app.db.models import AgentOutputRecord, PMProposalRecord

        outs = (
            s.scalars(
                select(AgentOutputRecord).where(AgentOutputRecord.cycle_id == chosen.id)
            ).all()
            if chosen
            else []
        )
        props = (
            s.scalars(select(PMProposalRecord).where(PMProposalRecord.cycle_id == chosen.id)).all()
            if chosen
            else []
        )
    plans = {d.id: _plan_view(d) for d in decs}
    with new_session() as s2:
        pos_rows = (
            s2.execute(
                select(Position.decision_id, Position.track).where(
                    Position.decision_id.in_([d.id for d in decs])
                )
            ).all()
            if decs
            else []
        )
    opened_by: dict[int, list[str]] = {}
    for dec_id, track in pos_rows:
        if dec_id is not None:
            opened_by.setdefault(dec_id, []).append(track)
    horizon = datetime.now(UTC) - timedelta(hours=get_config().trading.cycle_hours)
    why = {
        d.id: why_no_trade(d, sorted(opened_by.get(d.id, [])), expired=d.ts < horizon) for d in decs
    }
    return render(
        request,
        "decisions.html",
        focus=decision,
        suppressed=suppressed,
        user=user,
        cycles=cycles,
        chosen=chosen,
        decs=decs,
        hits=hits,
        outs=outs,
        props=props,
        plans=plans,
        why=why,
    )


BLOCK_WORDS = {
    "short_needs_borrow": "conviction too low to borrow for a short",
    "short_unavailable": "this pair cannot be shorted",
    "cost_rule": "target too close to cover the costs",
    "no_room": "exposure cap is full",
    "correlated_exposure": "exposure cap is full",
    "correlated_exposure_total": "exposure cap is full",
    "gross_exposure": "exposure cap is full",
    "liquidation_buffer": "stop too close to the liquidation level",
    "emergency_brake": "emergency brake is on",
    "paused": "trading is paused",
    "kill": "kill switch is on",
    "trades_per_day": "daily trade limit for this coin reached",
    "drawdown_pause": "paused after a drawdown",
    "valid_agents": "too few valid readings",
    "leverage_cap": "no margin available",
}
BLOCKING = ("block", "close_all", "brake", "pause")


def why_no_trade(d: DecisionRecord, opened: list[str], expired: bool = False) -> str:
    """A short note for the decision log when the managers called a direction and no
    trade (or not every ledger's trade) followed: the risk rule that blocked it, or what
    each ledger's executor ran into."""
    if d.direction == "flat":
        return ""
    if d.action == "none":
        blocks = [
            h
            for h in (d.risk_rule_hits or [])
            if h.get("effect") in BLOCKING and h.get("rule") != "no_consensus"
        ]
        words = []
        for h in blocks:
            rule = str(h.get("rule") or "")
            word = BLOCK_WORDS.get(rule) or BLOCK_WORDS.get(rule.split(":")[0])
            if rule.startswith("day_loss_stop"):
                word = "this direction is locked for the day (sleeve loss stop)"
            words.append(word or rule.replace("_", " "))
        return (
            "blocked: " + ", ".join(dict.fromkeys(words)) if words else "blocked by the risk rules"
        )
    notes = d.exec_notes or {}
    by_reason: dict[str, list[str]] = {}
    for track, text in notes.items():
        if track not in opened:
            by_reason.setdefault(str(text), []).append(track)
    parts = [f"opened on {', '.join(opened)}"] if opened else []
    parts += [f"{reason} ({', '.join(tracks)})" for reason, tracks in by_reason.items()]
    if not parts and d.action == "open":
        # the executor only acts on plans younger than one cycle
        return (
            "approved, never opened (reason not recorded)"
            if expired
            else "approved, not opened yet"
        )
    return " · ".join(parts)


def _plan_view(d: DecisionRecord) -> list[dict[str, Any]]:
    """Human-readable rows for the sized plan(s) of a decision, for the overlay."""
    out = []
    for key, label in (
        ("plan", "Primary track (live rules)"),
        ("plan_max", "Comparison track (max leverage)"),
    ):
        plan = (d.proposal or {}).get(key)
        if not plan:
            continue
        entry, stop, target = (Decimal(plan[k]) for k in ("entry", "stop", "target"))
        stop_pct = abs(stop - entry) / entry * 100
        target_pct = abs(target - entry) / entry * 100
        lev = Decimal(plan["leverage"])
        out.append(
            {
                "label": label,
                "direction": d.direction,
                "entry": fmt_price(entry),
                "stop": (
                    f"{fmt_price(stop)} ({stop_pct:.2f}% away, {stop_pct * lev:.2f}% of margin)"
                ),
                "target": (
                    f"{fmt_price(target)} ({target_pct:.2f}% away, "
                    f"{target_pct / stop_pct if stop_pct else 0:.1f}× the stop distance)"
                ),
                "leverage": f"{lev:g}x"
                + (" with borrowing" if plan.get("borrow") else ", no borrowing"),
                "notional": f"{Decimal(plan['notional']):,.2f} USDT",
                "margin": f"{Decimal(plan['margin']):,.2f} USDT own capital",
                "hold": f"at most {plan['max_hold_hours']} hours, then a time-stop",
                "exits": (
                    "stop, target, trailing stop (armed after +1R, trails 1R), time-stop, "
                    "liquidation, kill"
                ),
            }
        )
    return out


@app.get("/params", response_class=HTMLResponse)
def params(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    get_config.cache_clear()  # the executor rewrites config.toml; show what is in it now
    cfg = get_config()
    current = {k: _get_param(cfg, k) for k in EDITABLE}
    with new_session() as s:
        history = s.scalars(select(ParamChange).order_by(ParamChange.id.desc()).limit(50)).all()
        audit_rows = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(100)).all()
        aggressive = aggressive_view(cfg, s.get(RiskState, 1))
    return render(
        request,
        "params.html",
        user=user,
        editable=EDITABLE,
        current=current,
        aggressive=aggressive,
        history=history,
        audit=audit_rows,
        stepped=stepped_up(request),
    )


def _fresh_config() -> Any:
    get_config.cache_clear()
    return get_config()


def _get_param(cfg: Any, key: str) -> Any:
    section, name = key.split(".", 1)
    return getattr(getattr(cfg, section), name)


# --- actions (all require step-up TOTP) -------------------------------------------


def _require_step_up(request: Request) -> None:
    if not stepped_up(request):
        raise HTTPException(status_code=303, headers={"Location": f"/?stepup={request.url.path}"})


@app.post("/params")
def params_post(
    request: Request,
    key: str = Form(),
    value: str = Form(),
    csrf: str = Form(),
    user: str = Depends(current_user),
) -> Response:
    check_csrf(request, csrf)
    _require_step_up(request)
    if key not in EDITABLE:
        raise HTTPException(status_code=400, detail="not editable")
    lo, hi = EDITABLE[key]
    try:
        v = Decimal(value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="not a number") from exc
    if not (lo <= v <= hi):
        raise HTTPException(status_code=400, detail=f"{key} must be within {lo} and {hi}")
    with new_session() as s:
        s.add(
            ParamChange(
                ts=datetime.now(UTC),
                key=key,
                old_value=str(_get_param(_fresh_config(), key)),
                new_value=str(v),
                requested_by=user,
            )
        )
        auth.audit(s, user, "param_change", _ip(request), {"key": key, "value": str(v)})
    notify.send(
        "dorkbot admin: parameter change",
        f"{user} set {key} = {v} (applied by the executor within bounds)",
    )
    return RedirectResponse("/params", status_code=303)


@app.post("/params/aggressive")
def aggressive_post(
    request: Request,
    on: str = Form(),
    csrf: str = Form(),
    user: str = Depends(current_user),
) -> Response:
    """The aggressive-mode switch (owner 2026-10-11). It only flips a flag in the risk
    state; the profile itself is in config.toml and the risk engine applies it, inside
    hard bounds, from the next cycle on. Needs the step-up code like every action."""
    check_csrf(request, csrf)
    _require_step_up(request)
    want = on == "1"
    now = datetime.now(UTC)
    with new_session() as s:
        state = s.get(RiskState, 1)
        if state is None:
            state = RiskState(id=1, leverage_ceiling=Decimal(2))
            s.add(state)
        before = bool(state.aggressive)
        state.aggressive = want
        state.aggressive_since = now if want else None
        if want:
            state.day_locked_sleeves = {}  # no locks in this mode: clear what was set
        s.add(
            ParamChange(
                ts=now,
                key="aggressive mode",
                old_value="on" if before else "off",
                new_value="on" if want else "off",
                requested_by=user,
                applied_at=now,
                result="applied",
            )
        )
        auth.audit(s, user, "aggressive_mode", _ip(request), {"on": want})
        s.commit()
    notify.send(
        f"dorkbot admin: aggressive mode {'ON' if want else 'off'}",
        f"{user} switched aggressive mode {'on' if want else 'off'}. It applies from the next "
        "cycle. Open positions keep the terms they were opened with.",
    )
    return RedirectResponse("/params", status_code=303)


def aggressive_view(cfg: Config, state: RiskState | None) -> dict[str, Any]:
    """What the switch changes, normal next to aggressive, for the parameters page."""
    from app.risk.apply import RISK_HARD_MAX, sleeve_values

    agg = cfg.profile
    rows = []
    for name, s_cfg in cfg.sleeves.items():
        risk, stop, beta = sleeve_values(s_cfg, agg)
        rows.append(
            {
                "sleeve": name,
                "risk": (s_cfg.risk_per_trade, risk),
                "stop": (s_cfg.day_loss_stop, stop),
                "beta": (s_cfg.net_beta_exposure_max, beta),
                "lev": (
                    min(
                        Decimal(s_cfg.leverage_max), state.leverage_ceiling if state else Decimal(2)
                    ),
                    min(Decimal(s_cfg.leverage_max), Decimal(agg.leverage_ceiling)),
                ),
            }
        )
    return {
        "on": bool(state and state.aggressive),
        "since": state.aggressive_since if state else None,
        "rows": rows,
        "hold": agg.max_hold_hours,
        "risk_cap": RISK_HARD_MAX,
        "take_profit": agg.take_profit_equity_pct,
        "goal_gap": agg.goal_cycle_min_gap_minutes,
        "take_trade": agg.take_profit_trade_pct,
        "keep": agg.take_profit_keep,
        "stop_room": agg.stop_room,
    }


@app.post("/control/{kind}")
def control(
    request: Request,
    kind: str,
    csrf: str = Form(),
    reason: str = Form(""),
    user: str = Depends(current_user),
) -> Response:
    check_csrf(request, csrf)
    _require_step_up(request)
    if kind not in ("kill", "pause", "resume"):
        raise HTTPException(status_code=400)
    with new_session() as s:
        s.add(
            ControlRequest(
                ts=datetime.now(UTC), kind=kind, source=f"admin:{user}", reason=reason or None
            )
        )
        auth.audit(s, user, f"control_{kind}", _ip(request), {"reason": reason})
        s.commit()
    notify.send(f"dorkbot admin: {kind}", f"{user} requested {kind}. Reason: {reason or '-'}")
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health() -> dict[str, Any]:
    now = datetime.now(UTC)
    with new_session() as s:
        hb = {h.process: (now - h.ts).total_seconds() for h in s.scalars(select(Heartbeat)).all()}
        eq = s.execute(
            select(EquitySnapshot.equity)
            .where(EquitySnapshot.mode == "paper:primary")
            .order_by(EquitySnapshot.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
    return {
        "ok": all(v < 300 for v in hb.values()) and bool(hb),
        "heartbeat_age_s": hb,
        "equity_primary": str(eq) if eq is not None else None,
    }


def generate_secret() -> str:
    return secrets.token_urlsafe(48)
