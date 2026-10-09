"""Email alerts for the admin (new-IP login, parameter change, kill switch).

Provider from .env: ``ALERT_EMAIL_TO`` plus either ``RESEND_API_KEY`` or ``SMTP_URL``
(smtp://user:pass@host:port). Without either, alerts are logged and dropped.
"""

from __future__ import annotations

import base64
import html
import logging
import re
import smtplib
from email.message import EmailMessage
from urllib.parse import urlparse

import httpx

from app.config import get_secrets

log = logging.getLogger("alerts")


def send(
    subject: str,
    body: str,
    html_part: str | None = None,
    attachments: list[tuple[str, str]] | None = None,
) -> bool:
    """Every mail to the owner in one style: the plain text is also rendered as HTML with
    the wordmark (the memo's renderer), unless an HTML part is given. Text attachments as
    (filename, content)."""
    html_part = html_part or to_html(body)
    s = get_secrets()
    to = s.alert_email_to
    if not to:
        log.warning("alert (no recipient configured): %s", subject)
        return False
    if s.resend_api_key.get_secret_value():
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {s.resend_api_key.get_secret_value()}"},
            json={
                "from": s.alert_email_from or "dorkbot <alerts@dorkbot.dev>",
                "to": [to],
                "subject": subject,
                "text": body,
                "html": html_part,
                **(
                    {
                        "attachments": [
                            {"filename": name, "content": base64.b64encode(data.encode()).decode()}
                            for name, data in attachments
                        ]
                    }
                    if attachments
                    else {}
                ),
            },
            timeout=15,
        )
        if r.status_code >= 300:
            log.error("alert mail refused by Resend (%s): %s", r.status_code, r.text[:200])
        return r.status_code < 300
    if s.smtp_url:
        u = urlparse(s.smtp_url)
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s.alert_email_from or u.username or to, to, subject
        msg.set_content(body)
        msg.add_alternative(html_part, subtype="html")
        for name, data in attachments or []:
            msg.add_attachment(data.encode(), maintype="text", subtype="markdown", filename=name)
        with smtplib.SMTP(u.hostname or "localhost", u.port or 587, timeout=15) as smtp:
            smtp.starttls()
            if u.username:
                smtp.login(u.username, u.password or "")
            smtp.send_message(msg)
        return True
    log.warning("alert (no provider configured): %s", subject)
    return False


# --- rendering: the memo's small markdown as mail HTML -----------------------------

_STYLE = (
    "font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;"
    "line-height:1.5;color:#1a1a1a;max-width:720px;margin:0 auto;padding:16px"
)


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", text)


def to_html(markdown: str) -> str:
    """The memo's own small markdown (headings, bullets, numbered items with sub-bullets,
    bold, code) as clean HTML for mail clients. No library: the dialect is ours."""
    out: list[str] = []
    stack: list[str] = []  # open list tags

    def close_to(depth: int) -> None:
        while len(stack) > depth:
            out.append(f"</{stack.pop()}>")

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("# "):
            close_to(0)
            out.append(f"<h1 style='font-size:20px;margin:18px 0 8px'>{_inline(line[2:])}</h1>")
        elif line.startswith("## "):
            close_to(0)
            h2 = "font-size:16px;margin:22px 0 6px;color:#c85a00"
            out.append(f"<h2 style='{h2}'>{_inline(line[3:])}</h2>")
        elif re.match(r"^\d+\. ", line):
            close_to(0) if not stack or stack[0] != "ol" else close_to(1)
            if not stack:
                out.append("<ol style='padding-left:22px'>")
                stack.append("ol")
            out.append(f"<li style='margin:8px 0'>{_inline(line.split('. ', 1)[1])}")
        elif line.startswith("   - "):
            if len(stack) == 1:
                out.append("<ul style='margin:4px 0 4px 0;padding-left:18px'>")
                stack.append("ul")
            out.append(f"<li>{_inline(line[5:])}</li>")
        elif line.startswith("- "):
            if not stack or stack[0] != "ul":
                close_to(0)
                out.append("<ul style='padding-left:22px'>")
                stack.append("ul")
            out.append(f"<li style='margin:3px 0'>{_inline(line[2:])}</li>")
        else:
            close_to(0)
            out.append(f"<p style='margin:8px 0'>{_inline(line)}</p>")
    close_to(0)
    src = "https://dorkbot.dev/brand/logo-1200.png?v=6d8a4f25"
    logo = (
        f"<a href='https://dorkbot.dev/'><img src='{src}' alt='dorkbot' width='300' "
        "style='width:300px;max-width:100%;height:auto;display:block;margin:0 0 12px'></a>"
    )
    return f"<div style='{_STYLE}'>" + logo + "\n".join(out) + "</div>"
