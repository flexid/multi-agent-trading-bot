"""Email alerts for the admin (new-IP login, parameter change, kill switch).

Provider from .env: ``ALERT_EMAIL_TO`` plus either ``RESEND_API_KEY`` or ``SMTP_URL``
(smtp://user:pass@host:port). Without either, alerts are logged and dropped.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from urllib.parse import urlparse

import httpx

from app.config import get_secrets

log = logging.getLogger("alerts")


def send(subject: str, body: str) -> bool:
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
            },
            timeout=15,
        )
        return r.status_code < 300
    if s.smtp_url:
        u = urlparse(s.smtp_url)
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s.alert_email_from or u.username or to, to, subject
        msg.set_content(body)
        with smtplib.SMTP(u.hostname or "localhost", u.port or 587, timeout=15) as smtp:
            smtp.starttls()
            if u.username:
                smtp.login(u.username, u.password or "")
            smtp.send_message(msg)
        return True
    log.warning("alert (no provider configured): %s", subject)
    return False
