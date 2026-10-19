#!/usr/bin/env bash
# Build the static site and deploy it to Cloudflare Pages (project "competition", which
# serves dex.dorkbot.dev). Uses the same Cloudflare credentials as dorkbot.dev, read from the
# repo's .env. NEXT_PUBLIC_DEX_DATA is taken from the environment or
# from DEX_DATA_URL in that .env; without it the site reads its sample files.
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE="${ENV_FILE:-../.env}"
if [ -f "$ENV_FILE" ]; then set -a; source "$ENV_FILE"; set +a; fi
export NEXT_PUBLIC_DEX_DATA="${NEXT_PUBLIC_DEX_DATA:-${DEX_DATA_URL:-}}"
npm run build
npx --yes wrangler@4 pages deploy out --project-name dex --branch main
