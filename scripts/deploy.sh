#!/usr/bin/env bash
# Deploy to the droplet: sync the repo and .env, build, migrate, start scheduler + executor.
#   scripts/deploy.sh [root@164.90.211.109]
set -euo pipefail
HOST="${1:-root@164.90.211.109}"
DIR=/opt/dorkbot
cd "$(dirname "$0")/.."
# Before the restart the script waits for a running decision cycle to finish (and holds
# back just before a scheduled one): a restart mid-cycle loses the cycle.
# One SSH connection for the whole deploy (the server rate-limits new SSH connections).
CTL="/tmp/dorkbot-deploy-$$"
SSH="ssh -o ControlMaster=auto -o ControlPath=$CTL -o ControlPersist=120"
export RSYNC_RSH="$SSH"
trap 'ssh -o ControlPath=$CTL -O exit "$HOST" 2>/dev/null || true' EXIT
$SSH "$HOST" 'command -v docker >/dev/null || (curl -fsSL https://get.docker.com | sh); mkdir -p '"$DIR"
rsync -az --delete --exclude .git --exclude .venv --exclude pgdata --exclude logs --exclude '.env*' \
  --exclude config.toml --exclude __pycache__ --exclude .pytest_cache --exclude .mypy_cache \
  --exclude .ruff_cache --exclude node_modules --exclude .next --exclude out --exclude .wrangler \
  ./ "$HOST:$DIR/"
# The server's config.toml carries the owner's parameter changes (admin → executor): copy
# the repo's only when the server has none. New keys are merged by hand.
rsync -az --ignore-existing config.toml "$HOST:$DIR/"
scripts/admin_env.sh && scp -q -o ControlPath=$CTL .env .env.admin "$HOST:$DIR/"
$SSH "$HOST" "chmod 600 $DIR/.env $DIR/.env.admin && cd $DIR && docker compose --profile bot build -q && (docker compose exec -T scheduler python -m app.decision.idle; s=\$?; if [ \$s -eq 3 ]; then echo 'deploy stopped: a decision cycle is still running'; exit 1; fi; [ \$s -eq 0 ] || echo 'idle check unavailable, restarting anyway'; true) && docker compose --profile bot up -d && (docker builder prune -f --keep-storage 4gb >/dev/null 2>&1 || true) && docker compose --profile bot ps"
