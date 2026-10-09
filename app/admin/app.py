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
from sqlalchemy import func, select

from app.admin import auth, notify
from app.agents.polymarket import coverage as pm_coverage
from app.config import get_config, get_secrets
from app.db.models import (
    AdminUser,
    AuditLog,
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
    XPostOut,
    XPostRecord,
)
from app.db.session import new_session

app = FastAPI(title="dorkbot admin", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(Path(__file__).with_name("static"))), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))


def fmt_price(value: Any) -> str:
    """Prices with at most 4 decimals and thousands separators (owner, 2026-10-09)."""
    if value is None or value == "":
        return "-"
    try:
        q = Decimal(str(value)).quantize(Decimal("0.0001"))
    except Exception:
        return str(value)
    text = f"{q:,.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


SIDE_WORD = re.compile(r"\b(long|short)\b", re.IGNORECASE)


def side_words(value: Any) -> Markup:
    """Colour the words "long" (green) and "short" (red) wherever they appear
    (owner, 2026-10-09). The text is escaped first, so it is safe on raw reasons."""
    text = escape("" if value is None else str(value))
    return Markup(
        SIDE_WORD.sub(lambda m: f'<span class="{m.group(1).lower()}">{m.group(1)}</span>', text)
    )


templates.env.filters["price"] = fmt_price
templates.env.filters["side"] = side_words
COOKIE = "dorkbot_admin"
STEP_UP = "dorkbot_stepup"
CSRF = "dorkbot_csrf"

# Parameters the owner may change from the admin, with the hard bounds the executor
# enforces again in code. Leverage never above 10 (SPEC §12 M8b).
EDITABLE: dict[str, tuple[Decimal, Decimal]] = {
    "trading.leverage_max": (Decimal(1), Decimal(20)),  # owner 2026-10-09: settable to 20x
    "trading.leverage_max_spx6900": (Decimal(1), Decimal(10)),  # and SPX6900 to 10x
    "trading.risk_per_trade": (Decimal("0.001"), Decimal("0.02")),
    "trading.capital_share_per_asset": (Decimal("0.05"), Decimal("0.5")),
    "trading.gross_exposure_max": (Decimal(1), Decimal(3)),
    "trading.day_loss_stop": (Decimal("-0.05"), Decimal("-0.005")),
    "trading.depth_cap": (Decimal("0.01"), Decimal("0.1")),
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


def current_user(request: Request) -> str:
    data = _sessions().read(request.cookies.get(COOKIE))
    if not data:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return str(data["u"])


def stepped_up(request: Request) -> bool:
    data = _sessions().read(request.cookies.get(STEP_UP), max_age=auth.STEP_UP_TTL_S)
    return bool(data and data.get("su"))


def check_csrf(request: Request, csrf: str) -> None:
    token = request.cookies.get(COOKIE) or ""
    if not _sessions().csrf_ok(token, csrf):
        raise HTTPException(status_code=403, detail="bad csrf")


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    token = request.cookies.get(COOKIE) or ""
    csrf = _sessions().csrf(token) if token else ""
    return templates.TemplateResponse(request, name, {"csrf": csrf, **ctx})


# --- login -------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


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
    resp = RedirectResponse("/login", status_code=303)
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


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, user: str = Depends(current_user)) -> HTMLResponse:
    now = datetime.now(UTC)
    cfg = get_config()
    with new_session() as s:
        ledgers = s.scalars(select(PaperAccount).order_by(PaperAccount.id)).all()
        open_pos = s.scalars(
            select(Position)
            .where(Position.status.in_(["open", "closing"]))
            .order_by(Position.opened_at)
        ).all()
        recent = s.scalars(
            select(Position)
            .where(Position.status == "closed")
            .order_by(Position.closed_at.desc())
            .limit(30)
        ).all()
        heartbeats = s.scalars(select(Heartbeat)).all()
        sources = s.scalars(select(DataSource).order_by(DataSource.name)).all()
        risk = s.get(RiskState, 1)
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
        pending_ctl = s.execute(
            select(func.count())
            .select_from(ControlRequest)
            .where(ControlRequest.applied_at.is_(None))
        ).scalar_one()
        pm_cov = [pm_coverage(s, a, now) for a in cfg.trading.assets]
    x_month = Decimal(reads_month) * Decimal("0.005") + Decimal(posts_month) * Decimal("0.015")
    stale = {h.process: (now - h.ts) > timedelta(minutes=5) for h in heartbeats}
    return render(
        request,
        "overview.html",
        user=user,
        now=now,
        cfg=cfg,
        ledgers=ledgers,
        open_pos=open_pos,
        recent=recent,
        heartbeats=heartbeats,
        stale=stale,
        sources=sources,
        pm_cov=pm_cov,
        risk=risk,
        llm_day=llm_day,
        llm_month=llm_month,
        by_model=by_model,
        x_month=x_month,
        reads_month=reads_month,
        posts_month=posts_month,
        budget=cfg.budget.api_usd_per_month,
        pending_ctl=pending_ctl,
        stepped=stepped_up(request),
    )


@app.get("/decisions", response_class=HTMLResponse)
def decisions(
    request: Request, user: str = Depends(current_user), cycle: int | None = None
) -> HTMLResponse:
    with new_session() as s:
        cycles = s.scalars(select(Cycle).order_by(Cycle.id.desc()).limit(40)).all()
        chosen = s.get(Cycle, cycle) if cycle else (cycles[0] if cycles else None)
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
    return render(
        request,
        "decisions.html",
        user=user,
        cycles=cycles,
        chosen=chosen,
        decs=decs,
        hits=hits,
        outs=outs,
        props=props,
        plans=plans,
    )


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
    cfg = get_config()
    current = {k: _get_param(cfg, k) for k in EDITABLE}
    with new_session() as s:
        history = s.scalars(select(ParamChange).order_by(ParamChange.id.desc()).limit(50)).all()
        audit_rows = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(100)).all()
    return render(
        request,
        "params.html",
        user=user,
        editable=EDITABLE,
        current=current,
        history=history,
        audit=audit_rows,
        stepped=stepped_up(request),
    )


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
                old_value=str(_get_param(get_config(), key)),
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
