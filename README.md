# swordbot

A personal customer-support assistant. You describe an order problem to a Telegram bot ("My DoorDash order was missing the fries"). The bot works out the details, drafts an email to the merchant's support team, and sends it from your Gmail only after you press **Send**. When support replies, the bot picks the case back up. It asks you before making any consequential decision, such as accepting store credit instead of a refund.

> Status: early development. The scaffold, the case domain (database, state machine, facts with provenance), the event queue with its worker, and the Telegram adapter and the LLM client layer exist. For now the bot only echoes messages back with a test button; the intake conversation and email come next. See [docs/PLAN.md](docs/PLAN.md) for the roadmap.

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

Main tables so far: `users`, `support_cases` (status, fact-backed fields, optimistic-lock `version`), `case_facts` (append-only facts with `source` provenance), `case_transitions` (audit log of every status change), `events` (the inbox and job queue, below) and `pending_actions` (the records behind chat buttons). The allowed status transitions are listed in [docs/PLAN.md](docs/PLAN.md#case-state-machine).

## Events and the worker

Every input becomes a row in `events` first: Telegram updates and Gmail notifications (from M3 and M10), and scheduled work such as follow-ups. A worker inside the app process then handles each one ([app/events/](app/events/), PLAN D1):

- **Duplicates are dropped.** `(source, external_id)` is unique, so a redelivered Telegram update or Gmail notification is ignored.
- **One at a time per user, in order.** A user's events never run concurrently, and a newer event waits while an older one is retrying. Different users' events run in parallel on Postgres.
- **Retries.** A failing event is retried with backoff (15s, 30s, 1m, … capped at 30m) for 8 attempts, about 30 minutes, and is then marked `dead` and reported. Errors that can never succeed on retry (`PermanentEventError`, such as an invalid payload) go straight to `dead`.
- **Crash safety.** A claimed event holds a 5-minute lease. If the process dies, the event is retried once the lease expires. A handler's database writes commit only together with the event being marked done. On a normal shutdown, in-flight events are handed back immediately.
- **Scheduling.** Future work is an event with a later `run_at`.

To push a synthetic event while the app is running (it is created for `TELEGRAM_ALLOWED_USER_ID` unless you pass `--telegram-user-id`):

```bash
uv run python scripts/inject_event.py --type user_message --external-id demo-1
uv run python scripts/inject_event.py --type user_message --external-id demo-1   # "duplicate"
uv run python scripts/inject_event.py --type user_message --delay 20             # runs ~20s later
```

A `user_message` event is answered in Telegram, so it needs `TELEGRAM_BOT_TOKEN`; give it text with `--payload '{"text": "hi"}'`. Event types without a handler go `dead` with `UnknownEventTypeError`. Inspect the queue with `docker compose exec db psql -U swordbot -c "select id, type, status, attempts, run_at, last_error from events order by id"`.

With SQLite the worker handles one event at a time, and a long handler holds SQLite's single write lock. Use Postgres for anything beyond tests.

## Environment variables

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | `development`, `test`, or `production`. Production turns on JSON logs and turns off `/docs`. |
| `LOG_LEVEL` | Standard log level name |
| `APP_BASE_URL` | Public HTTPS URL of the deployment, used to register webhooks |
| `DATABASE_URL` | SQLAlchemy async URL: `postgresql+asyncpg://…`, or `sqlite+aiosqlite:///…` for local development |
| `TEST_DATABASE_URL` | Tests only. A disposable Postgres database; when set, DB tests also run against Postgres |
| `WORKER_ENABLED` | Run the event worker in the app process (default `true`) |
| `WORKER_CONCURRENCY` | Events processed at once, across different users (default `4`; always 1 on SQLite) |
| `WORKER_POLL_INTERVAL_SECONDS` | How often the worker checks for due events (default `1.0`) |
| `EVENT_MAX_ATTEMPTS` | Attempts before an event is marked `dead` (default `8`) |
| `EVENT_RETRY_BASE_SECONDS`, `EVENT_RETRY_MAX_SECONDS` | Exponential backoff start and cap (defaults `15` and `1800`) |
| `EVENT_LEASE_SECONDS` | How long a claimed event may run before it is presumed crashed (default `300`) |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | LLM access and model name (default `gpt-5`) |
| `OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_RETRIES` | Per-request timeout and the SDK's own quick retries (defaults `45` and `1`) |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | Secret that Telegram echoes back in `X-Telegram-Bot-Api-Secret-Token` |
| `TELEGRAM_ALLOWED_USER_ID` | The only Telegram user the bot responds to (your numeric id) |
| `TELEGRAM_API_BASE_URL` | Bot API base URL (default `https://api.telegram.org`) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` | Gmail OAuth client |
| `GMAIL_REFRESH_TOKEN` | Produced by the one-time OAuth script (M7) |

Secrets are loaded as `SecretStr` and are never logged. Log output also passes through a redaction step (`app/logging.py`).

## LLM

All model calls go through `LLMClient` ([app/llm/](app/llm/), PLAN D4 and D11), on the OpenAI Responses API. Only `app/llm/openai_client.py` imports the `openai` SDK.

- **Two calls.** `complete` returns free text. `extract_structured` returns a Pydantic model: the request carries a strict JSON schema, the reply is validated in code, and invalid output gets **one** repair retry. If it is still invalid, the event fails with `InvalidAgentDecisionError` and you get a Telegram notice. Agent steps (M5) are structured outputs too.
- **Stateless, not stored.** Every request sends the full context and sets `store=false`, so OpenAI keeps no stored response to chain from; the database owns conversation state. Only the minimum content needed for the task is sent.
- **Errors.** Timeouts, connection errors, 5xx and rate limits are temporary and retried by the worker. A bad key (401/403), an exhausted quota (`insufficient_quota`), a rejected request (such as an unknown model) or a refusal is permanent. Error messages carry only the status and OpenAI's error code.
- **Logs.** Each call logs an `llm_call` line with purpose, model, schema, attempts, duration and token usage, never prompts or output.

To check your key and model against the real API (one or two small calls):

```bash
uv run python scripts/llm_smoke.py
uv run python scripts/llm_smoke.py --text "Amazon sent me the wrong charger"
```

It prints the extracted `ExtractedIssue` as JSON, or a typed error such as `LLMAuthenticationError: 401: invalid_api_key`.

## Gmail OAuth setup

_To be written in M7._ Scopes will be `gmail.send` (M7) and `gmail.readonly` (M8+). The OAuth consent screen must be set to **In production**, because in Testing mode refresh tokens expire after 7 days.

## Telegram bot setup

The bot is its own Telegram account, created with @BotFather. You talk to it from your normal account. It can only see messages sent to it directly, and it answers only `TELEGRAM_ALLOWED_USER_ID`; everyone else is ignored without a reply. Chats with bots are not end-to-end encrypted, so order details and email drafts pass through Telegram's servers.

1. In Telegram, message **@BotFather**, send `/newbot`, and pick a display name and a username ending in `bot`. Put the token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send @BotFather `/setjoingroups`, pick your bot, and choose **Disable**, so nobody can add it to a group. The bot ignores group chats anyway.
3. Open your bot's chat and press **Start**. A bot can't message you until you do.
4. Find your numeric Telegram user id: run the poller below with `TELEGRAM_ALLOWED_USER_ID` empty and message the bot. The rejection log line shows `sender_id` (never the message). Put it in `TELEGRAM_ALLOWED_USER_ID`.
5. Set `TELEGRAM_WEBHOOK_SECRET` to a random string of letters, digits, `_` and `-` (webhook mode only, used from M7.5): `py -3.14 -c "import secrets; print(secrets.token_urlsafe(32))"`.

If the token ever leaks, send @BotFather `/revoke` and update `.env`.

**Local development uses polling.** Telegram can only deliver webhooks to a public HTTPS URL, so locally a second process fetches updates:

```bash
uv run uvicorn app.main:app --reload     # terminal 1: API + worker
uv run python scripts/telegram_poll.py   # terminal 2: Telegram -> events
```

The poller deletes any registered webhook first (Telegram refuses polling while one is set) and passes each update through the same code as the webhook. Send the bot a message: it echoes it back with a **Test button**. Pressing the button once confirms it; pressing it again says it is no longer valid.

**Production uses the webhook** `POST /telegram/webhook` (set up in M7.5). It is disabled (404) unless `TELEGRAM_WEBHOOK_SECRET` is set, and rejects requests whose `X-Telegram-Bot-Api-Secret-Token` header doesn't match (401).

How it works ([app/telegram/](app/telegram/), PLAN D1, D3 and D10):

- **Inbound.** Each update is authorized (the allowed user, in a private chat) and stored as a `user_message` or `user_button_action` event, deduplicated on the message or button-press id. Updates that aren't messages or button presses are dropped.
- **Outbound.** Handlers never call Telegram directly. Each reply, button acknowledgement or button removal is queued as a `telegram_outbound` event in the handler's transaction and delivered by the worker. A Telegram outage retries only the delivery. If a message can't be delivered at all, you get a short notice once Telegram works again.
- **Buttons.** Each button is a `pending_actions` row; the button carries only its id. A press is accepted once, only from you, only before it expires and only while its case is still in the expected status. Pressing one button closes the others shown with it.
- **Secrets.** The bot token is part of every Bot API URL, so the `httpx` request logger is kept at WARNING, Telegram errors never include the URL, and anything shaped like a bot token is scrubbed from log values.

## Deployment

The app is a container (see `Dockerfile`) that listens on `$PORT`. It needs an always-on process, because the event worker runs in-process (see PLAN D1). Run `alembic upgrade head` as a release step before starting the new version; the image includes the migrations. _Details will be written in M7.5._
