#!/usr/bin/env bash
# Build the static site and deploy it to Cloudflare Pages (project "competition", domain
# competition.dorkbot.dev). Uses the same Cloudflare credentials as dorkbot.dev, read from the
# repo's .env. NEXT_PUBLIC_COMPETITION_API is taken from the environment or
# from COMPETITION_API_URL in that .env; without it the site reads its sample files.
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE="${ENV_FILE:-../.env}"
if [ -f "$ENV_FILE" ]; then set -a; source "$ENV_FILE"; set +a; fi
export NEXT_PUBLIC_COMPETITION_API="${NEXT_PUBLIC_COMPETITION_API:-${COMPETITION_API_URL:-}}"
npm run build
npx --yes wrangler@4 pages deploy out --project-name competition --branch main
