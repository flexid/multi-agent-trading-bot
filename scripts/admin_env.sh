#!/usr/bin/env bash
# Derive .env.admin from .env: only the keys the admin container is allowed to see.
set -euo pipefail
cd "$(dirname "$0")/.."
grep -E '^(DATABASE_URL|ADMIN_SECRET_KEY|ALERT_EMAIL_TO|ALERT_EMAIL_FROM|RESEND_API_KEY|SMTP_URL)=' .env > .env.admin || true
chmod 600 .env.admin
