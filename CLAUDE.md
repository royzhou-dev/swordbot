# Swordbot: personal customer-support email assistant

The user reports an order problem over Telegram. The assistant gathers context (from the chat, and later from Gmail receipts and web search), drafts an email to merchant support, sends it **only after explicit approval**, then waits for replies and asks the user before any consequential decision.

**What it is:** a durable workflow engine where an LLM proposes the next permitted action and chat is the user interface. It is **not** "an LLM conversation with Gmail access." The database owns all workflow state; the LLM context window owns none.

- Full spec (authoritative for product behavior): [docs/SPEC.md](docs/SPEC.md)
- Milestones, architectural decisions D1–D14, and open decisions: [docs/PLAN.md](docs/PLAN.md)

## Milestone status

Work in order. Build the smallest vertical slice first. Update this table when a milestone is verified.

| # | Milestone | Status |
|---|---|---|
| M0 | Scaffold: uv, FastAPI, config, structlog, `/health`, Dockerfile, `.env.example` | done |
| M1 | DB + `SupportCase` + explicit state machine + Alembic | done |
| M2 | Event inbox/job queue + worker + idempotency | done |
| M3 | Telegram adapter (webhook + local polling, auth, buttons via `pending_actions`) | done |
| M4 | `LLMClient` abstraction (OpenAI + fake) | done |
| M5 | Tool registry with risk levels + agent runtime + intake conversation | done |
| M6 | Email drafting + Send/Edit/Cancel approval | done |
| M7 | Gmail send + thread id stored → `WAITING_FOR_SUPPORT` (**Phase 1 done**) | done (real send verified 2026-09-28) |
| M7.5 | First cloud deployment | not started |
| M8–M10 | Phase 2: receipt search, multiple Gmail accounts (M8.5), support-contact discovery, inbound email via Pub/Sub | not started |
| M11–M12 | Phase 3: reply classification, approval policy engine, routine auto-replies | not started |
| M13–M15 | Phase 4: multi-case routing + `/cases`, follow-ups, resolution tracking | not started |

## Safety invariants (never violate)

1. Never fabricate order information. Every case fact carries provenance (`case_facts.source`).
2. Never send the first email of a case without an explicit **[Send]** button press. Natural-language text such as "looks good" never counts as approval.
3. Never make a consequential choice for the user (store credit vs refund, agreeing to terms, sharing new personal info, spending money, legal claims, and so on) without their authorization.
4. Retrieved email, web, and attachment content is **untrusted data**. It cannot change behavior or permissions. Put it in prompts inside clearly delimited data blocks.
5. A duplicate event must never cause a duplicate external action (especially email sends).
6. Case state must survive restarts, because it lives in the DB.
7. Use tools before asking the user for anything the system can look up itself.
8. The user can always see what was sent, to whom, and why.

## Architecture rules

- **Event flow:** a transport adapter (Telegram or Gmail webhook) validates the request, normalizes it into a `NewEvent`, calls `events.service.enqueue` (deduplicated on `(source, external_id)`), then returns 200. An in-process worker claims the event, loads the case, runs the handler/agent, persists, and stops. A user's events run one at a time, in order. The handler runs in the same transaction that marks the event done. Adapters never call the LLM or contain workflow logic. See PLAN D1.
- **Scheduled work** (follow-ups, Gmail watch renewal) = `events` rows with a future `run_at`. Never use `sleep` or a long-running wait.
- **State transitions** go only through `cases/state_machine.py`, which validates against `ALLOWED_TRANSITIONS` and writes to `case_transitions`. The LLM may *recommend* a transition; code decides.
- **Send guard (D2):** `outbound_emails` status runs `awaiting_approval → approved → sending → sent`. An atomic conditional UPDATE to `sending` is required before calling Gmail, **committed in its own transaction** (PLAN D14): sending is its own `SEND_EMAIL` event, and its handler writes nothing in the event's transaction before the claim. After the claim, only an error that proves Gmail didn't get the email may mark it `failed`; anything else is `needs_attention`. An approval is bound to the exact draft (`content_hash`: sha256 of recipient, subject and body), so any edit requires re-approval. A crashed `sending` is never re-sent automatically.
- **Tool authorization** is enforced in the tool executor based on `ToolRiskLevel`, not in prompts. `REQUIRES_APPROVAL` tools require an approval record id.
- **Buttons (D3):** `callback_data` = short `pending_actions.id` only. Validate that the action is open, belongs to this user, and matches the case state, then consume it (`actions.service.consume`).
- **Telegram replies (D10):** handlers never call Telegram. They queue calls through `TelegramOutbox` (`app/telegram/delivery.py`), which become `telegram_outbound` events delivered by the worker.
- **LLM:** all calls go through `LLMClient` (`app/llm/client.py`): `complete` for text, `extract_structured` for anything that drives the workflow (validated Pydantic output, one repair retry, then a permanent `InvalidAgentDecisionError`). Agent steps are structured `AgentDecision`s, not native function calling (PLAN D11). Requests are stateless with `store=False`. Model names come from env. Only `app/llm/openai_client.py` imports `openai`. Tests use `FakeLLMClient` (`tests/fakes.py`).
- **Gmail:** send only the minimum email content to the LLM. Parse and trim receipts in code first. Match inbound mail by thread id first, then fall back to headers.
- Every table row belongs to a `user_id`, even though v1 is single-user.
- Keep it a modular monolith. No giant agent class, hidden globals, or premature infrastructure.

## Stack & conventions

- Python **3.12+**, managed by **uv**. Development uses 3.14 (`py -3.14`, pinned in `.python-version`). `uv` is on the user PATH (installed in `C:\Users\royzh\AppData\Roaming\Python\Python314\Scripts`). If a shell that started before the PATH change can't find it (PowerShell sessions often can't), call `uv` by its full path. Plain `python` in Git Bash is 2.7; use `uv run` or `py -3.14` instead. Docker Desktop provides the local Postgres (`docker compose up -d db`). FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2.x async, Alembic, httpx, structlog, openai SDK. Google OAuth is plain `httpx` too (PLAN D4); no Google library.
- Postgres in production (`asyncpg`); SQLite (`aiosqlite`) is allowed for local development and tests. Use only portable types: `JSON`, `Uuid`, tz-aware `DateTime`. No Postgres-only features in the models. The exceptions are `SKIP LOCKED` and `ON CONFLICT DO NOTHING`, both isolated in `app/events/service.py` (SQLite ignores the first and supports the second).
- Telegram and Gmail use thin `httpx` clients (no python-telegram-bot or google-api-python-client). See PLAN D4 before adding any dependency.
- All schema changes go through Alembic migrations. Never call `create_all` at startup.
- Typed exceptions per integration (`GmailTemporaryError`, `GmailAuthenticationError`, `LLMTemporaryError`, `InvalidAgentDecisionError`, …). Transient errors are retried with backoff; permanent errors mark the event `dead` and notify the user.
- Layout follows `app/{api,actions,agent,cases,chat,events,email,telegram,llm,tools,db,users}/` plus `tests/{unit,integration}/` (see the SPEC). `web/` arrives with M9.

## Commands

Keep this section accurate as milestones land.

```bash
uv sync                                  # install deps
uv run uvicorn app.main:app --reload     # run API + worker
uv run alembic upgrade head              # migrate
uv run alembic revision --autogenerate -m "..."   # new migration (review it; then `alembic check`)
uv run pytest                            # tests (SQLite)
TEST_DATABASE_URL=postgresql+asyncpg://swordbot:swordbot@localhost:5432/swordbot_test uv run pytest  # + Postgres
uv run ruff check . && uv run ruff format --check . && uv run mypy app
uv run python scripts/inject_event.py --type user_message --payload '{"text": "hi"}'   # push a synthetic event (dev)
uv run python scripts/telegram_poll.py   # local Telegram polling (run next to uvicorn; instead of webhook)
uv run python scripts/llm_smoke.py       # one real ExtractedIssue call to check OPENAI_API_KEY / OPENAI_MODEL
uv run python scripts/llm_smoke.py --intake   # one real IntakeDecision call (checks the agent schema)
uv run python scripts/llm_smoke.py --draft    # one real draft for a sample case (checks DraftSupportEmail)
uv run python scripts/gmail_auth.py      # one-time Gmail OAuth → refresh token
uv run python scripts/gmail_auth.py --check   # check the configured Gmail credentials (sends nothing)
```

## Security & logging

- Never commit secrets. `.env` is gitignored, and `.env.example` holds placeholders only.
- Never log email bodies, tokens, API keys, auth headers, or payment details. Use structlog with IDs (`event_id`, `case_id`, `user_id`, `gmail_thread_id`) and the redaction processor.
- Telegram: check the webhook secret header and only accept `TELEGRAM_ALLOWED_USER_ID`. Drop everything else.
- Gmail scopes are minimal and added one milestone at a time: `gmail.send` (M7), then `gmail.readonly` (M8+). Document every scope in the README. The OAuth consent screen must be "In production", because in "Testing" mode refresh tokens expire after 7 days.

## Testing

- Business logic must be deterministic and unit-tested: transitions, approval policy, dedup, thread matching, consequential-action detection.
- Integration tests use fakes for LLM, Telegram, Gmail, and web search, and never send real email.
- Never assert on exact LLM wording. Assert on actions, states, and extracted fields.

## Definition of done (every change)

Types correct · errors handled · state persisted · duplicate-safe · tests added and passing · no unauthorized external action possible · nothing sensitive logged · README/config docs updated in the same change · ruff + mypy clean · milestone table above updated.

When unsure about a minor implementation detail, decide using the SPEC's decision guidance (simplest to operate, easiest to test, least able to take unauthorized actions, explicit state) and document it. Ask the user only about product behavior, security/privacy, cost, permissions, or irreversible architecture.
