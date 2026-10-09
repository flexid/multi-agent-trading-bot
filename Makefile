.PHONY: up down deploy deploy-site snapshot golive costs selftest-live-full test lint migrate fetch-once scheduler cycle executor kill resume indicators backtest selftest selftest-live bybit-authorize

up:
	docker compose up -d postgres

deploy:
	scripts/deploy.sh $(HOST)

deploy-site:
	scripts/deploy_site.sh

snapshot:
	uv run python -m app.site.publish

down:
	docker compose down

test:
	uv run pytest -q

migrate:
	uv run alembic upgrade head

# Data layer: one pass of every fetch job, or the 15-minute scheduler.
fetch-once:
	uv run python -m app.scheduler --once

scheduler:
	uv run python -m app.scheduler

# Decision cycle, executor and controls.
cycle:
	uv run python -m app.decision.cycle $(ARGS)

executor:
	uv run python -m app.execution.executor $(ARGS)

kill:
	uv run python -m app.control kill --reason "$(REASON)"

resume:
	uv run python -m app.control resume

# M9: go-live verdict, cost report, live self-test (server only; real orders at minimal size).
golive:
	uv run python -c "from app.config import get_config; from app.db.session import new_session; from app.risk.golive import evaluate; \
	  s=new_session(); v=evaluate(s, get_config()); print('READY' if v.ready else 'not yet'); [print(('ok  ' if c.ok else 'FAIL'), c.name, '=', c.value) for c in v.criteria]"

costs:
	uv run python -c "from app.ops import cost_report; print(cost_report())"

selftest-live-full:
	uv run python -m app.execution.live_selftest --confirm "$(CONFIRM)"

# Indicators agent on stored candles, and its backtest.
indicators:
	uv run python -m app.agents.run_indicators $(ARGS)

backtest:
	uv run python -m app.backtest $(ARGS)

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

# Connectivity + paper round-trip. Never places a real order.
selftest:
	uv run python -m app.selftest $(ARGS)

# One minimal real order + cancel. Requires CONFIRM=yes.
selftest-live:
	uv run python -m app.selftest --live --confirm "$(CONFIRM)" $(ARGS)

# Connect to a Bybit AI subaccount via OAuth; writes the key into .env.
bybit-authorize:
	uv run python -m app.bybit_authorize $(ARGS)
