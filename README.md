# swordbot

A personal customer-support assistant. You describe an order problem to a Telegram bot ("My DoorDash order was missing the fries"). The bot works out the details, drafts an email to the merchant's support team, and sends it from your Gmail only after you press **Send**. When support replies, the bot picks the case back up. It asks you before making any consequential decision, such as accepting store credit instead of a refund.

> Status: early development. The scaffold and the case domain (database, state machine, facts with provenance) exist; there is no chat or email integration yet. See [docs/PLAN.md](docs/PLAN.md) for the roadmap.

## Architecture

The system is a durable workflow engine. Chat is the interface, and the LLM proposes the next permitted action. External inputs (Telegram messages and button presses, Gmail notifications, scheduled follow-ups) become events stored in Postgres. A worker processes each event against the support case's explicit state machine. Every outbound email needs an approval record, which the code checks. See [docs/SPEC.md](docs/SPEC.md) for the full specification and [docs/PLAN.md](docs/PLAN.md) for the design decisions.

## Local setup

Requirements: Python 3.12 or newer (developed on 3.14), [uv](https://docs.astral.sh/uv/), and Docker Desktop for the Postgres database.

```bash
cp .env.example .env            # then fill in values
uv sync                         # creates .venv and installs dependencies
docker compose up -d db         # local Postgres
uv run alembic upgrade head     # create/upgrade the schema
uv run uvicorn app.main:app --reload
curl http://localhost:8000/health
```

To run without Docker, point `DATABASE_URL` at a SQLite file instead, for example `sqlite+aiosqlite:///./swordbot.db`. Production always uses Postgres.

## Running checks

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy app
```

Every database test runs against SQLite. To also run them against Postgres (CI always does), create a throwaway test database and set `TEST_DATABASE_URL`. The tests wipe that database, so never point it at real data.

```bash
docker compose exec db createdb -U swordbot swordbot_test
TEST_DATABASE_URL=postgresql+asyncpg://swordbot:swordbot@localhost:5432/swordbot_test uv run pytest
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs ruff, mypy and the full test suite against a Postgres service on every push to `main` and every pull request.

## Database and migrations

The schema is owned by Alembic ([migrations/](migrations/)). The app never creates tables itself, and tests build their databases by running the migrations.

To change the schema:

1. Edit the models (`app/*/models.py`; `app/db/models.py` imports them all so Alembic sees them).
2. Generate a revision against Postgres: `uv run alembic revision --autogenerate -m "describe the change"`.
3. Review the generated file by hand. Keep it portable (no Postgres-only types), since it also runs on SQLite.
4. `uv run alembic upgrade head`, then `uv run alembic check` should report no differences. The test suite checks this too.

Main tables so far: `users`, `support_cases` (status, fact-backed fields, optimistic-lock `version`), `case_facts` (append-only facts with `source` provenance) and `case_transitions` (audit log of every status change). The allowed status transitions are listed in [docs/PLAN.md](docs/PLAN.md#case-state-machine).

## Environment variables

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | `development`, `test`, or `production`. Production turns on JSON logs and turns off `/docs`. |
| `LOG_LEVEL` | Standard log level name |
| `APP_BASE_URL` | Public HTTPS URL of the deployment, used to register webhooks |
| `DATABASE_URL` | SQLAlchemy async URL: `postgresql+asyncpg://…`, or `sqlite+aiosqlite:///…` for local development |
| `TEST_DATABASE_URL` | Tests only. A disposable Postgres database; when set, DB tests also run against Postgres |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | LLM access and model name |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | Secret that Telegram echoes back in `X-Telegram-Bot-Api-Secret-Token` |
| `TELEGRAM_ALLOWED_USER_ID` | The only Telegram user the bot responds to |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` | Gmail OAuth client |
| `GMAIL_REFRESH_TOKEN` | Produced by the one-time OAuth script (M7) |

Secrets are loaded as `SecretStr` and are never logged. Log output also passes through a redaction step (`app/logging.py`).

## Gmail OAuth setup

_To be written in M7._ Scopes will be `gmail.send` (M7) and `gmail.readonly` (M8+). The OAuth consent screen must be set to **In production**, because in Testing mode refresh tokens expire after 7 days.

## Telegram bot setup

_To be written in M3._

## Deployment

The app is a container (see `Dockerfile`) that listens on `$PORT`. It needs an always-on process, because the event worker runs in-process (see PLAN D1). Run `alembic upgrade head` as a release step before starting the new version; the image includes the migrations. _Details will be written in M7.5._
