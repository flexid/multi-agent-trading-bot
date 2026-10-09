#!/usr/bin/env bash
# Deploy to the droplet: sync the repo and .env, build, migrate, start scheduler + executor.
#   scripts/deploy.sh [root@164.90.211.109]
set -euo pipefail
HOST="${1:-root@164.90.211.109}"
DIR=/opt/dorkbot
cd "$(dirname "$0")/.."
ssh "$HOST" 'command -v docker >/dev/null || (curl -fsSL https://get.docker.com | sh); mkdir -p '"$DIR"
rsync -az --delete --exclude .git --exclude .venv --exclude pgdata --exclude logs --exclude '.env*' \
  --exclude config.toml --exclude __pycache__ --exclude .pytest_cache --exclude .mypy_cache \
  --exclude .ruff_cache ./ "$HOST:$DIR/"
# The server's config.toml carries the owner's parameter changes (admin → executor): copy
# the repo's only when the server has none. New keys are merged by hand.
rsync -az --ignore-existing config.toml "$HOST:$DIR/"
scripts/admin_env.sh && scp -q .env .env.admin "$HOST:$DIR/"
ssh "$HOST" "chmod 600 $DIR/.env $DIR/.env.admin && cd $DIR && docker compose --profile bot build -q && docker compose --profile bot up -d && docker compose --profile bot ps"
