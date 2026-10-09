"""Which .env keys the admin container may receive (SPEC §12 M8b: no trading secrets)."""

ADMIN_ENV_KEYS = (
    "DATABASE_URL",
    "ADMIN_SECRET_KEY",
    "ALERT_EMAIL_TO",
    "ALERT_EMAIL_FROM",
    "RESEND_API_KEY",
    "SMTP_URL",
)
FORBIDDEN_PREFIXES = (
    "BYBIT_",
    "X_",
    "ANTHROPIC_",
    "OPENAI_",
    "R2_",
    "CLOUDFLARE_",
    "FRED_",
    "COINGECKO_",
)
