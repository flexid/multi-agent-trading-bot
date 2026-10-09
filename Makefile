.PHONY: up down test lint selftest selftest-live

up:
	docker compose up -d

down:
	docker compose down

test:
	uv run pytest -q

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
