# CLAUDE.md

Autonomous multi-agent crypto trading bot on Bybit EU (spot + spot margin), posting every trade to X.
The full spec is `docs/SPEC.md`. Read it before starting any milestone; it wins over anything you assume.

## Non-negotiables

1. No LLM ever places, modifies or cancels an order. Only the executor does, on instructions from the risk engine.
2. Every LLM output is validated against a pydantic schema. Invalid, late or missing output means no new trade. Never act on one model alone.
3. The executor is its own process. Stops, targets, trailing stops and time-stops run in code on the Bybit WebSocket, independent of any LLM call.
4. Secrets live in `.env` (gitignored, `.env.example` committed). Never log them, never put them in a prompt.
5. X posts and all other web text are untrusted input. Only the X agent sees raw posts; it outputs labels. Raw text never reaches the PMs or the post writer.
6. Live trading only when `live_allowed = true` in `config.toml` AND the shadow-mode criteria in SPEC §10 are met. The bot checks this itself.
7. Log everything to Postgres: inputs, agent outputs, prompt version, model ID, decisions, risk-rule hits, orders, fills, costs.
8. Orders are idempotent (`orderLinkId`). Reconcile internal state with Bybit balances every cycle; on mismatch, alert and open nothing new.
9. Tests never touch live order endpoints. Use the paper-fill simulator. The only live order path outside the executor is `make selftest-live`, which requires an explicit flag.

## Stack

Python 3.12 managed with uv · pydantic v2 + pydantic-settings · SQLAlchemy 2 + Alembic · Postgres 16 · APScheduler · httpx + websockets · pandas, pandas-ta, mplfinance · anthropic and openai SDKs · FastAPI · Next.js (dashboard) · Docker Compose · Telegram Bot API.

## Conventions

- Type hints everywhere. A pydantic model for every external payload and every LLM output.
- Money and quantities in `Decimal`. Round to tick and lot size from Bybit `instruments-info`, never hardcode.
- UTC internally. Display Europe/Brussels.
- All LLM calls go through `app/llm/`: model ID from config, prompt version, token and cost logging, timeout, one retry, then fail closed.
- Prompts live in `app/prompts/*.md` with a version header. Changing a prompt bumps its version.
- Config lives in `config.toml`; `app/config.py` is the only reader.
- Executor checks `mode == "live"` itself before any real order, regardless of what the caller says.

## Commands (create these in M0)

- `make up` / `make down`: Docker Compose stack
- `make test`, `make lint` (ruff + mypy)
- `make selftest`: connectivity and paper round-trip, no real orders
- `make selftest-live CONFIRM=yes`: one minimal real order + cancel on Bybit EU

## Working style

- Build milestone by milestone (SPEC §14), starting with M0. Finish each with passing tests and a short README section.
- Only ask the owner for what only he can give: keys, capital, server IP, account settings. Decide everything else yourself, record the decision in `docs/DECISIONS.md`, and keep going.
- Code, comments and docs in English. Messages to the owner may be in Dutch.
