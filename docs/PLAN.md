# Build Plan

This file breaks [SPEC.md](SPEC.md) into milestones. Each milestone is small enough to build, test, and verify on its own. Each one leaves the system working. Work them in order. The Phase 1 vertical slice (M0–M7) comes before any inbound-email work.

Status is tracked in the table in [../CLAUDE.md](../CLAUDE.md). Update it when a milestone is done.

Every milestone must meet the spec's Definition of Done: types, error handling, persisted state, duplicate-safety, tests, no unauthorized actions, no sensitive logging, docs updated, and `ruff` + `mypy` + `pytest` all passing.

---

## Key architectural decisions

These were decided up front so that every milestone builds on the same foundation. Change them only on purpose, and update this section when you do.

### D1. Durable event inbox + in-process worker
- Webhooks (Telegram, Gmail Pub/Sub) only **validate, normalize, and insert** a row into `events`, then return 200 right away. They never call the LLM or Gmail inline.
- `events` has a unique constraint on `(source, external_id)`, for example `("telegram", update_id)` or `("gmail", message_id)`. A duplicate delivery hits the unique constraint and is dropped. This is the first layer of idempotency.
- A background worker runs inside the FastAPI process (started from the app lifespan). It claims pending events whose `run_at <= now()`. On Postgres it uses `SELECT … FOR UPDATE SKIP LOCKED`; on SQLite, a single worker with a conditional status update.
- Events for the **same case are processed serially**: the worker never processes two events for one case at the same time.
- Retries: transient errors reschedule with backoff (`attempts`, `run_at`, `last_error`). Permanent errors, or too many attempts, mark the event `dead` and notify the user. Events are never silently dropped.
- **Scheduled work (follow-ups, Gmail watch renewal) uses the same table** with a future `run_at`, so there is no separate scheduler. This is the "persist scheduled work behind an abstraction" requirement.
- Consequence: the deploy target must run a long-lived process (Railway, Render, Fly.io, or a Cloud Run service with min-instances=1 and CPU always allocated). This is documented in the README.

### D2. The outbound email state machine is the send guard
- Every outbound email is an `outbound_emails` row. Its status moves `draft → awaiting_approval → approved → sending → sent | failed | cancelled`.
- An approval is tied to one exact draft, using `body_hash` plus subject and recipient. Any edit creates a new draft version that needs new approval.
- To send, the code first runs an atomic conditional update (`UPDATE … SET status='sending' WHERE id=? AND status='approved'`). If no rows were updated, it does not send. This is what makes a duplicate button press or webhook harmless (Invariant 5).
- Each email gets a client-generated RFC 822 `Message-ID` before sending. If the process crashes while an email is `sending`, the email is **never automatically re-sent**. The system checks Gmail Sent for that Message-ID (once the read scope exists, from M8 on). Before that, it marks the email `needs_attention` and asks the user.
- The `send_support_email` tool refuses to run without a valid approval record. This check lives in code, not in the prompt (Invariant 2).

### D3. Telegram buttons use durable action records
- Telegram limits `callback_data` to 64 bytes. Each button therefore points to a row in `pending_actions` (`id`, `user_id`, `case_id`, `kind`, `payload`, `status`, `expires_at`), and `callback_data` carries only a short action id.
- A button press becomes a `USER_BUTTON_ACTION` event carrying that action id. The handler checks that the action is still `open`, belongs to the user, and matches the case's current state, then consumes it. Old or already-used buttons do nothing and reply "this is no longer valid".

### D4. Thin HTTP clients, not SDK frameworks
- **Telegram**: a small `httpx` async client plus Pydantic models for the Update fields we use. This avoids python-telegram-bot or aiogram, which bring their own event loop and dispatcher that would conflict with our event layer.
- **Gmail**: `google-auth` for OAuth tokens, plus `httpx` calls to the Gmail REST API. `google-api-python-client` is synchronous and heavy.
- **OpenAI**: the official `openai` SDK, wrapped behind the `LLMClient` protocol. Workflow-driving outputs use structured outputs (Pydantic schemas). Model names come from config.
- **Tests**: a fake `LLMClient`, fake Telegram, Gmail and web-search clients (protocol implementations), and `respx` for HTTP-level client tests.

### D5. Tooling
- `uv` manages dependencies. The project needs Python 3.12+; development uses 3.14 (`.python-version`).
- `ruff` for lint and format, `mypy --strict` on `app/`, `pytest` + `pytest-asyncio`.
- `pydantic-settings` for typed config and `structlog` for JSON logs, with a redaction processor for tokens, auth headers and email bodies.
- Migrations: Alembic, async SQLAlchemy 2.x. Postgres uses `asyncpg`; local development and tests may use SQLite via `aiosqlite`. Use only portable column types: `JSON` (not `JSONB` in the models), `Uuid`, timezone-aware `DateTime`. CI runs the test suite against Postgres too.

### D6. Gmail OAuth for a single user (v1)
- A one-time local script, `scripts/gmail_auth.py`, runs the installed-app OAuth flow and prints a refresh token. That token is stored as a secret (`GMAIL_REFRESH_TOKEN`). OAuth tokens are moved into an encrypted DB table only when multi-user support arrives.
- Scopes are added one milestone at a time and documented in the README:
  - M7: `https://www.googleapis.com/auth/gmail.send`
  - M8+: add `gmail.readonly` (search, read, history, watch)
  - Add `gmail.compose` only if we start creating real Gmail drafts. v1 keeps drafts in our DB, so it is not needed.
- Gotcha: an OAuth app left in **"Testing"** publishing status gets refresh tokens that **expire after 7 days**. Move the consent screen to "In production" (unverified, personal use). The README must document this.

### D7. Routing messages to a case
- v1 keeps a per-user "focused case". A new complaint while no case is focused creates a case. Replies while a case is in `GATHERING_CONTEXT` or `WAITING_FOR_USER` go to that case.
- Once there are several active cases (M13), an LLM classifier returns `{case_id | new_case | ambiguous}`. If the result is ambiguous, the bot asks with buttons. It never guesses when a decision is being applied.

### D8. Case facts, provenance, and optimistic locking (decided in M1)
- `case_facts` is **append-only**. The current value of a key is its latest row, and a JSON `null` clears it. Facts are never updated in place, so the history of where each value came from is kept (Invariant 1).
- Some facts are mirrored into `support_cases` columns for easy querying (`merchant_name`, `merchant_domain`, `issue_type`, `issue_summary`, `desired_resolution`, `order_number`, `order_date`, `support_email`). `cases.service.set_fact` is the **only** writer of those columns, so a column always equals the latest fact for its key. `update_fields` accepts only operational fields (`gmail_thread_id`, `auto_reply_enabled`).
- `support_cases.version` is SQLAlchemy's `version_id_col`: every UPDATE bumps it, and a write based on a stale read raises `ConcurrentCaseUpdateError`. The caller rolls back and retries the unit of work.
- `focused` is a boolean guarded by a partial unique index (`user_id WHERE focused`, portable to SQLite), so each user has at most one focused case. `focus_case` moves focus, and closing a case clears it.
- The log tables (`case_facts`, `case_transitions`) use integer ids so their rows have a total order. Entity tables use UUIDs.
- Services take an `AsyncSession` and never commit. The caller owns the transaction (`Database.transaction()`).

---

## Case state machine

Defined in `app/cases/state_machine.py` (`ALLOWED_TRANSITIONS`). This table mirrors it.

| From | Allowed to |
|---|---|
| `GATHERING_CONTEXT` | `READY_TO_DRAFT` |
| `READY_TO_DRAFT` | `WAITING_FOR_USER_APPROVAL`, `GATHERING_CONTEXT` |
| `WAITING_FOR_USER_APPROVAL` | `READY_TO_SEND` (Send pressed), `READY_TO_DRAFT` (redraft), `GATHERING_CONTEXT` |
| `READY_TO_SEND` | `WAITING_FOR_SUPPORT` (sent), `WAITING_FOR_USER_APPROVAL` (send failed or needs attention) |
| `WAITING_FOR_SUPPORT` | `PROCESSING_SUPPORT_REPLY`, `READY_TO_DRAFT` (follow-up, M14), `RESOLVED` |
| `PROCESSING_SUPPORT_REPLY` | `WAITING_FOR_USER`, `READY_TO_REPLY`, `WAITING_FOR_SUPPORT` (e.g. auto-acknowledgement), `RESOLVED` |
| `WAITING_FOR_USER` | `READY_TO_REPLY`, `WAITING_FOR_SUPPORT`, `RESOLVED` |
| `READY_TO_REPLY` | `WAITING_FOR_USER_APPROVAL`, `WAITING_FOR_SUPPORT` (routine auto-reply, M12) |
| `RESOLVED` | `PROCESSING_SUPPORT_REPLY` (support wrote again), `WAITING_FOR_USER` (user reopened) |
| `CANCELLED` | none (terminal) |
| `ERROR` | any active status (explicit recovery, reason required), `CANCELLED` |

Additional rules:
- Every **active** status (all except `RESOLVED`, `CANCELLED` and `ERROR`) can also move to `CANCELLED` or `ERROR`.
- Cancelling stops tracking the case. **Nothing is sent to support**, including from `WAITING_FOR_SUPPORT`. From M7 on, a cancel while an email is `sending` must be handled by the caller (D2).
- A resolved case can be reopened (user decision, M1). `resolved_at` is set on entering `RESOLVED` and cleared on leaving it.
- There are no self-transitions. Editing a draft keeps the case in `WAITING_FOR_USER_APPROVAL`; the new version lives in `outbound_emails`.
- Case creation is logged as a transition from `NULL` to `GATHERING_CONTEXT`.
- The state machine checks legality only. Preconditions such as "an approval record exists" belong to the caller (D2).

---

## Core tables (introduced over time)

| Table | Introduced | Purpose |
|---|---|---|
| `users` | M1 | `telegram_user_id` (unique), display name, signature name |
| `support_cases` | M1 | Spec fields, plus `version` (optimistic locking) and `focused` |
| `case_facts` | M1 | Append-only provenance (D8): `case_id, user_id, key, value(JSON), source, source_ref, confidence` |
| `case_transitions` | M1 | Audit log: from, to, reason, actor, event_id, at (Invariant 8). FK on `event_id` added in M2 |
| `case_messages` | M5 | Chat log for LLM context. Not workflow state. |
| `events` | M2 | Inbox and job queue (D1) |
| `pending_actions` | M3 | Button actions (D3) |
| `outbound_emails` | M6 | Drafts, approvals and sends (D2) |
| `email_messages` | M10 | Inbound emails being tracked: unique `gmail_message_id`, thread, case, classification |
| `gmail_sync_state` | M10 | `history_id` and watch expiration per user |

---

## Phase 1 — First vertical slice

### M0. Project scaffold
- `pyproject.toml` (uv), `app/main.py` FastAPI app with lifespan, `app/config.py` (pydantic-settings), structlog setup with redaction, `GET /health`.
- `.env.example`, `.gitignore` (covering `.env`, tokens and credentials), `Dockerfile`, `docker-compose.yml` (Postgres only), README skeleton, `git init`.
- **Verify:** `uv run pytest` passes a health test. `uv run uvicorn app.main:app` serves `/health`. `ruff` and `mypy` are clean.

### M1. Database and case domain
- `db/base.py`, `db/session.py`, the models above that are marked M1, and the first Alembic migration.
- `cases/state_machine.py`: an explicit `ALLOWED_TRANSITIONS` table and a `transition(case, to, reason, event_id)` function that validates the move, writes to `case_transitions`, and bumps `version`. It raises `InvalidTransitionError`.
- `cases/service.py`: create, get, update fields, and set a fact with its provenance.
- **Verify:** unit tests cover every allowed transition and reject disallowed ones. `alembic upgrade head` works on both SQLite and Postgres.

### M2. Event layer
- `events/models.py` (`EventType`, `Event` Pydantic model), `events/service.py` (insert that dedupes, claim, complete, fail and retry, schedule), `events/worker.py` (lifespan task, per-case serialization), and a handler registry in `events/handlers.py`.
- `scripts/inject_event.py` to push a synthetic event during local development.
- **Verify:** tests show that a duplicate `(source, external_id)` insert is a no-op, that a failing handler retries with backoff and then goes `dead`, and that two events for one case never run at the same time.

### M3. Telegram adapter
- `telegram/client.py` (sendMessage, editMessageText, answerCallbackQuery), `telegram/keyboards.py` (builds from `pending_actions`), `api/telegram.py` webhook.
- The webhook checks the `X-Telegram-Bot-Api-Secret-Token` header and `TELEGRAM_ALLOWED_USER_ID`. Unauthorized updates are dropped with a log entry that contains no message content.
- Local mode: `scripts/telegram_poll.py` long-polls `getUpdates` and feeds updates through **the same** normalization path.
- A temporary `USER_MESSAGE` handler echoes the message back and shows a test button.
- **Verify:** a real bot echoes a message and handles a button press end to end in polling mode. Tests cover the auth rejection, duplicate `update_id`, and stale-button handling.

### M4. LLM layer
- `llm/client.py`: the `LLMClient` protocol (`complete`, `extract_structured`, `run_agent`), an `OpenAIClient` implementation, and a `FakeLLMClient` for tests. Typed errors: `LLMTemporaryError`, `InvalidAgentDecisionError`.
- Every structured output is validated against its Pydantic schema. One repair retry is allowed, then it raises a typed error.
- **Verify:** unit tests with the fake client. `scripts/llm_smoke.py` runs `ExtractedIssue` extraction against the real API.

### M5. Agent runtime and intake conversation
- `tools/registry.py`: a `Tool` definition (name, Pydantic args and result, `ToolRiskLevel`). The executor enforces the risk level: `REQUIRES_APPROVAL` tools need an approval record id.
- `agent/runtime.py`: a bounded loop (at most N tool steps per event) that returns an `AgentDecision`. `agent/policies.py` holds the required fields for each issue type (for example, missing item needs merchant, order identifier, missing items and desired resolution). `agent/prompts.py` includes the untrusted-content policy.
- Handler flow for `USER_MESSAGE`: route to a case (D7), extract or merge facts (provenance = `user_message`), compute missing fields in code, and ask one short question per turn.
- **Verify:** an integration test with the fake LLM covers the complaint, one follow-up question, the answer, and the case reaching `READY_TO_DRAFT` with facts sourced correctly.

### M6. Drafting and approval
- A `draft_support_email` tool (LOW_RISK_WRITE) creates an `outbound_emails` row. Telegram shows the draft with **[Send] [Edit] [Cancel]**.
- Edit: the user types changes, the LLM revises, and the result is a new draft version with a new approval requirement. Cancel moves the case to `CANCELLED` after the user confirms.
- Until M9, the support email address comes from the user (the bot asks for it). Emails are signed with the user's configured name.
- **Verify:** tests show that text like "yeah looks good" never approves anything (only the button does), that an edit invalidates the old Send button, and that a double-pressed Send creates only one approval.

### M7. Gmail send — Phase 1 complete
- `email/gmail_client.py` (token refresh, `send_message`), `scripts/gmail_auth.py`, and the `send_support_email` tool (REQUIRES_APPROVAL) using the D2 send guard.
- Store `gmail_thread_id` and `gmail_message_id`, move the case to `WAITING_FOR_SUPPORT`, and confirm to the user in Telegram what was sent and to whom.
- **Verify:** a full integration test with fakes, including a duplicate approval event that produces one send. A manual run sends a real email to the user's own second address.

### M7.5. First deployment
- Pick a platform (see Open Decisions), set up a managed Postgres, set the Telegram webhook with a secret, and run migrations as a release step.
- **Verify:** the Phase 1 flow works from a phone with the laptop off.

---

## Phase 2 — Context gathering and inbound email

### M8. Gmail receipt search
- Add the `gmail.readonly` scope. Tools: `search_order_emails(merchant, approximate_date, order_number?)` returns `OrderEmailCandidate`s, and `read_email(message_id)`.
- Receipts are parsed in code first (HTML to text, then trimmed). Only the trimmed receipt text goes to `extract_structured` to produce `ReceiptInfo` (order number, date, total, items, and any support contact).
- Before any fact from a receipt is used, the bot asks "I found order #… for $… — is this it? [Yes] [No]". Provenance = `gmail_receipt`.
- **Verify:** fixture emails (DoorDash, Amazon, generic) extract correctly. The LLM only ever receives the minimal content.

### M9. Support contact discovery
- `web/search.py` defines a `WebSearchClient` protocol and one provider (see Open Decisions). `web/support_discovery.py` applies the priority order: receipt, then official domain, then official help center, then search results on official domains, then a fallback.
- An official domain is one that matches `merchant_domain`, which comes from the receipt sender or the user. Store the contact with its method, address, source URL, source type and confidence. If no email channel exists, tell the user which channel the merchant requires.
- **Verify:** unit tests with canned search results check that a third-party page's address is ranked below the official domain.

### M10. Inbound email tracking
- `api/gmail.py` is a Pub/Sub push endpoint that verifies the Google OIDC JWT. It runs `history.list` from the stored `history_id` and turns each new message into an `EMAIL_RECEIVED` event (deduped on `gmail_message_id`).
- A `users.watch` renewal is scheduled as a recurring event every 6 days (watches expire after 7). There is also a local-dev polling fallback.
- `email/threading.py` matches by thread id first, then falls back to In-Reply-To/References. Messages sent by the user are ignored.
- The handler summarizes the reply (untrusted content), notifies the user, and moves the case to `WAITING_FOR_USER`.
- **Verify:** a fake Gmail sequence of reply, duplicate notification, and unrelated email gives one notification for the correct case.

## Phase 3 — Bounded autonomy

### M11. Reply classification and consequential decisions
- `SupportReplyClassification` includes `SupportReplyType`, a confidence score, extracted offers and requested info. Compensation offers become button choices ([Refund] [Credit] [Other]).
- The answers go back through a new draft plus approval (all outbound email still needs approval).

### M12. Approval policy engine and routine auto-replies
- `agent/policies.py`: `classify_outbound(draft, case) -> ROUTINE | CONSEQUENTIAL`. A reply is routine only if every fact it discloses is already approved or known. Otherwise it is consequential.
- Auto-send happens only when `auto_reply_enabled` is on, the reply is routine, and the LLM confidence is above the threshold. Each auto-send notifies the user with the sent text.
- **Verify:** a table-driven test suite covers every consequential example in the spec.

## Phase 4 — Lifecycle

### M13. Multiple active cases and case routing (D7 classifier), plus `/cases`, `/help`, `/settings`.
### M14. Scheduled follow-ups: a `FOLLOW_UP_DUE` event is scheduled when a case enters `WAITING_FOR_SUPPORT` and cancelled or rescheduled when a reply arrives. The agent proposes a follow-up, which goes through approval.
### M15. Resolution extraction: a structured `Resolution` record, closing the case only when resolution is unambiguous, notifying the user, and a case history view.

---

## Open decisions (ask the user when the milestone arrives)

1. ~~**Local Postgres (M1)**~~ Decided: Docker Desktop, running `docker compose up -d db`.
2. **Hosting platform (M7.5):** must run a long-lived process (D1). Suggested options: Railway or Fly.io.
3. **Web search provider (M9):** OpenAI's built-in web search tool, Brave Search API, or Tavily. This affects cost and adds another API key.
4. **Signature/display name** used in emails (M6).
