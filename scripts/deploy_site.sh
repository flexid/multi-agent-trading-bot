#!/usr/bin/env bash
# Build the static site and deploy it to Cloudflare Pages (project "dorkbot", domain dorkbot.dev).
# Needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in .env, and SNAPSHOT_PUBLIC_URL.
set -euo pipefail
cd "$(dirname "$0")/../site"
set -a; source ../.env; set +a
export NEXT_PUBLIC_SNAPSHOT_URL="${SNAPSHOT_PUBLIC_URL:?set SNAPSHOT_PUBLIC_URL in .env}"
npm run build
npx --yes wrangler@4 pages deploy out --project-name dorkbot --branch main
