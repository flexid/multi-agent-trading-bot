# M8 report: public site (M8a) and private admin (M8b)

2026-10-09. **Built; waiting on Cloudflare to go public.** Lint, mypy and 132 tests pass.

## M8a: dorkbot.dev

| Piece | State |
| --- | --- |
| Snapshot (`app/site/snapshot.py`) | Explicit pydantic whitelist schema (`extra="forbid"`), never dumped rows. Performance vs BTC buy-and-hold and the equal-weight basket, drawdown, equity curve (primary track), stats, open trades (only after their X post), closed trades with the X thread link, agents per asset (scores, confidence, validity, whitelisted reason lines), Claude vs GPT, consensus, macro regime, coupling, tradfi and crypto-native parts, leaderboard (filled by M9), mode badge. `verify()` fails on any forbidden key or a string that breaks the number whitelist (site mode allows "nfa" and "bot"); tests cover a forbidden key, an amount, and the footer. |
| Publish job (`app/site/publish.py`) | Every 5 minutes from the scheduler: build, verify, render the 1200×630 OG image, push `snapshot.json` and `og.png` to R2 (S3 API). Without R2 credentials it writes `site/public/data/` locally. A failed snapshot never touches trading. |
| Site (`site/`) | Next.js 15 static export, dark, mobile-first, no backend: overview and agents pages fetch the snapshot URL client-side and refresh every 5 minutes; OG tags point at the R2 image; footer "nfa. just a bot trading its own bag." with the X link. `make deploy-site` builds and deploys to Cloudflare Pages via wrangler. |

## M8b: admin.dorkbot.dev

| Piece | State |
| --- | --- |
| Isolation | Own container, own env file (`.env.admin`, derived by `scripts/admin_env.sh` from a fixed allow-list: database, session key, alert mail). `tests/test_admin.py` asserts the compose service and the allow-list carry no Bybit, X, LLM or Cloudflare keys. The admin only writes `control_requests` and `param_changes`; the executor applies them within hard bounds (leverage ≤ 10, SPX6900 ≤ 3, listed bounds per key) into `config.toml`. |
| Auth (`app/admin/auth.py`) | Single user, argon2id password, TOTP (pyotp), lockout after 5 failures for 15 minutes, signed 30-minute session cookie (HttpOnly, SameSite=Strict, Secure except over a localhost SSH tunnel), CSRF token per session, TOTP step-up valid 5 minutes for kill, pause, resume and every parameter change. Email on login from a new IP, every parameter change and every control action (`app/admin/notify.py`: Resend or SMTP; silent until configured). |
| Pages | Overview: both ledgers with cash, realized, fees, interest, liquidations; open positions with sizes and liquidation prices; recent closes; LLM cost today and this month per model against the budget; X reads and posts; data freshness per source; heartbeats; controls. Decisions: per cycle the decisions, risk-rule hits, PM proposals (main and alt) and raw agent outputs. Parameters: editable keys with bounds, change history with the executor's result, audit log of every admin action. `/health` for monitoring. |
| Setup | `python -m app.admin.setup --username ...` on the server: password, QR, first-code confirmation. |
| Network | Admin bound to 127.0.0.1:8080; server firewall SSH-only (ufw, default deny incoming, 22/tcp rate-limited). Going public uses a Cloudflare Tunnel, so no inbound 443 is ever opened. Postgres and the admin publish on 127.0.0.1 only, so Docker's iptables rules expose nothing. SSH: keys only (`PermitRootLogin prohibit-password`, `PasswordAuthentication no`), fail2ban on sshd (5 tries, 1 h ban), unattended security upgrades on (2026-10-09). |

## Not yet

| Item | Waiting on |
| --- | --- |
| Site live at dorkbot.dev, snapshot on R2 | Cloudflare account ID, API token, R2 token, bucket with public domain. |
| Admin at admin.dorkbot.dev | Cloudflare Tunnel (same token). Until then: SSH tunnel to 127.0.0.1:8080. |
| Email alerts | Resend key or SMTP URL. |
| Leaderboard on the site | M9 scoring. |
| Last-backup indicator on the health page | Backup job comes with M9's ops items (nightly `pg_dump` to R2). |

## Needed from the owner

1. Run the admin setup over SSH and log in through the tunnel.
2. Cloudflare credentials, R2 token, bucket + public domain, `SNAPSHOT_PUBLIC_URL`.
3. Email provider credential.
