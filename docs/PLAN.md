# Build Plan

This file breaks [SPEC.md](SPEC.md) into milestones. Each milestone is small enough to build, test, and verify on its own. Each one leaves the system working. Work them in order. The Phase 1 vertical slice (M0–M7) comes before any inbound-email work.

Status is tracked in the table in [../CLAUDE.md](../CLAUDE.md). Update it when a milestone is done.

Every milestone must meet the spec's Definition of Done: types, error handling, persisted state, duplicate-safety, tests, no unauthorized actions, no sensitive logging, docs updated, and `ruff` + `mypy` + `pytest` all passing.

---

## Key architectural decisions

These were decided up front so that every milestone builds on the same foundation. Change them only on purpose, and update this section when you do.

### D1. Durable event inbox + in-process worker
- Webhooks (Telegram, Gmail Pub/Sub) only **validate, normalize, and insert** a row into `events`, then return 200 right away. They never call the LLM or Gmail inline.
- `events` has a unique constraint on `(source, external_id)`, for example `("telegram", "message:{chat_id}:{message_id}")`, `("telegram", "callback:{callback_query_id}")` or `("gmail", message_id)`. A duplicate delivery is dropped by `INSERT … ON CONFLICT DO NOTHING` (supported by both Postgres and SQLite). This is the first layer of idempotency.
  - Telegram keys use message and callback-query ids, not `update_id` (decided in M3). Telegram restarts `update_id` at a random value after a week without updates, so an old id could collide and silently drop a real message. Message ids are never reused within a chat.
- A background worker runs inside the FastAPI process (started from the app lifespan). It claims pending events whose `run_at <= now()`. On Postgres it uses `SELECT … FOR UPDATE SKIP LOCKED`; on SQLite, a single worker with a conditional status update.
- **Events for the same user are processed serially, in arrival order** (decided in M2; stricter than per case). Routing a message to a case happens inside the handler, so serializing per case would let two quick messages race to create or route cases. For a single-user bot this costs nothing noticeable, and the serialization key is just `events.user_id`.
  - The claim query picks the lowest `id` that is due and not blocked. An event is blocked while the same user has a `processing` event, or an older `pending` event that is due or waiting to retry. An event scheduled for the future blocks nothing until it is due.
  - A partial unique index (`user_id WHERE status = 'processing'`) makes two in-flight events for one user impossible even under a race.
- **Claims are leases.** Claiming sets `status='processing'`, a fresh `claim_token`, `locked_until = now + lease` (5 minutes), and increments `attempts`, so a crash mid-handler still counts. A handler is cut off at 90% of the lease. An expired lease (the process crashed or hung) is recovered as a failed attempt. Every later write to the event checks the `claim_token`, so a worker whose lease expired cannot complete it.
- **The handler and the event's completion share one transaction.** The handler's DB writes, including follow-on events it enqueues, commit together with `status='done'` or roll back together. External side effects can still repeat after a crash (at-least-once), which is why sends are guarded separately (D2).
- Retries: any exception is transient unless it is a `PermanentEventError`. Transient errors reschedule with exponential backoff (`attempts`, `run_at`, `last_error`): 15s, 30s, 1m, … capped at 30m, for 8 attempts, which is about 30 minutes (decided in M2; configurable). Permanent errors, or running out of attempts, mark the event `dead` and notify the user. Events are never silently dropped. `last_error` keeps only the exception type for third-party errors, because their messages can contain tokens or user content.
- On graceful shutdown, in-flight events get a short grace period (`WORKER_SHUTDOWN_GRACE_SECONDS`, default 10s, since M7.5) and are then released without counting the attempt. After a hard kill they wait for their lease to expire.
- **Scheduled work (follow-ups, Gmail watch renewal) uses the same table** with a future `run_at`, so there is no separate scheduler. This is the "persist scheduled work behind an abstraction" requirement.
- Consequence: the deploy target must run a long-lived process (Railway, Render, Fly.io, or a Cloud Run service with min-instances=1 and CPU always allocated). Railway was chosen in M7.5 (D15). This is documented in the README.

### D2. The outbound email state machine is the send guard
- Every outbound email is an `outbound_emails` row, one per version. Its status moves `awaiting_approval → approved → sending → sent | failed | needs_attention`. A version still awaiting approval can become `superseded` (replaced by a newer version, or discarded when the case goes back to intake), and an unsent one `cancelled` (decided in M6: there is no separate `draft` status, because a draft is shown for approval as soon as it is saved).
- An approval is tied to one exact draft through `content_hash`, a sha256 of recipient, subject and body together (from M8.5, the sending account too). Any edit creates a new version that needs new approval.
- To send, the code first runs an atomic conditional update (`UPDATE … SET status='sending' WHERE id=? AND status='approved'`). If no rows were updated, it does not send. This is what makes a duplicate button press or webhook harmless (Invariant 5).
- Each email gets a client-generated RFC 822 `Message-ID` before sending. If the process crashes while an email is `sending`, the email is **never automatically re-sent**. The system checks Gmail Sent for that Message-ID (once the read scope exists, from M8 on). Before that, it marks the email `needs_attention` and asks the user.
- The `send_support_email` tool refuses to run without a valid approval record. This check lives in code, not in the prompt (Invariant 2).

### D3. Telegram buttons use durable action records
- Telegram limits `callback_data` to 64 bytes. Each button therefore points to a row in `pending_actions` (`id`, `user_id`, `case_id`, `kind`, `payload`, `status`, `expires_at`), and `callback_data` carries only a short action id.
- A button press becomes a `USER_BUTTON_ACTION` event carrying that action id. The handler checks that the action is still `open`, belongs to the user, and matches the case's current state, then consumes it. Old or already-used buttons do nothing and reply "this is no longer valid".
- Details (decided in M3): `callback_data` is `a:` plus the action's UUID in hex (34 bytes). Buttons shown on one message share a `group_id`; consuming one marks the rest `superseded`, so a prompt is answered once. An expired action, or one whose case is no longer in `expected_case_status`, supersedes its whole group, so an old [Send] can never approve a later draft. Consumption is a conditional UPDATE (`open → consumed`) and records `consumed_by_event_id`. Code lives in `app/actions/`.

### D4. Thin HTTP clients, not SDK frameworks
- **Telegram**: a small `httpx` async client plus Pydantic models for the Update fields we use. This avoids python-telegram-bot or aiogram, which bring their own event loop and dispatcher that would conflict with our event layer.
- **Gmail**: `httpx` calls to the Gmail REST API and to Google's token endpoint. `google-api-python-client` is synchronous and heavy. (Changed in M7: the plan was `google-auth` for tokens, but its refresh is synchronous and needs `requests` or `aiohttp`, and a refresh-token grant is one form POST. Doing it in `httpx` keeps it async, maps its errors onto the D14 classes directly and tests with `respx`. No Google library is a dependency.)
- **OpenAI**: the official `openai` SDK, wrapped behind the `LLMClient` protocol. Workflow-driving outputs use structured outputs (Pydantic schemas). Model names come from config.
- **Tests**: a fake `LLMClient`, fake Telegram, Gmail and web-search clients (protocol implementations), and `respx` for HTTP-level client tests.

### D5. Tooling
- `uv` manages dependencies. The project needs Python 3.12+; development uses 3.14 (`.python-version`).
- `ruff` for lint and format, `mypy --strict` on `app/`, `pytest` + `pytest-asyncio`.
- `pydantic-settings` for typed config and `structlog` for JSON logs, with a redaction processor for tokens, auth headers and email bodies.
- Migrations: Alembic, async SQLAlchemy 2.x. Postgres uses `asyncpg`; local development and tests may use SQLite via `aiosqlite`. Use only portable column types: `JSON` (not `JSONB` in the models), `Uuid`, timezone-aware `DateTime`. CI runs the test suite against Postgres too.

### D6. Gmail OAuth for a single user (v1)
- A one-time local script, `scripts/gmail_auth.py`, runs the installed-app OAuth flow and prints a refresh token. It uses a "Desktop app" client, a loopback redirect (`GOOGLE_REDIRECT_URI`, default `http://127.0.0.1:8080/`), PKCE and a `state` check, built on the standard library and `httpx` (so no `google-auth-oauthlib`). `--check` refreshes a token with the configured credentials and sends nothing. That token is stored as a secret (`GMAIL_REFRESH_TOKEN`). OAuth tokens are moved into an encrypted DB table only when multi-user support arrives. Several Gmail accounts for the one user (M8.5) are still env/host secrets, one refresh token per account.
- Scopes are added one milestone at a time and documented in the README:
  - M7: `https://www.googleapis.com/auth/gmail.send`
  - M8+: add `gmail.readonly` (search, read, history, watch)
  - Add `gmail.compose` only if we start creating real Gmail drafts. v1 keeps drafts in our DB, so it is not needed.
- Gotcha: an OAuth app left in **"Testing"** publishing status gets refresh tokens that **expire after 7 days**. Move the consent screen to "In production" (unverified, personal use). The README must document this.

### D7. Routing messages to a case
- v1 keeps a per-user "focused case". A new complaint while no case is focused creates a case. Replies while a case is in `GATHERING_CONTEXT` or `WAITING_FOR_USER` go to that case.
- A message while the focused case is `WAITING_FOR_SUPPORT` (decided in M7) goes to intake with no case, as a possible new problem, but the intake model also gets a `sent_case` data block (merchant, recipient, subject, date sent, and a status note that replies aren't tracked yet). A question about the sent case gets a status reply. Code enforces that a `reply_to_user` there never opens a case, even if the model recorded facts, so "any news from DoorDash?" can't start a duplicate. A new problem opens a new case, which takes the focus; the sent case keeps waiting and is matched by thread id from M10. M10 replaces the status note.
- Once there are several active cases (M13), an LLM classifier returns `{case_id | new_case | ambiguous}`. If the result is ambiguous, the bot asks with buttons. It never guesses when a decision is being applied.

### D8. Case facts, provenance, and optimistic locking (decided in M1)
- `case_facts` is **append-only**. The current value of a key is its latest row, and a JSON `null` clears it. Facts are never updated in place, so the history of where each value came from is kept (Invariant 1).
- Some facts are mirrored into `support_cases` columns for easy querying (`merchant_name`, `merchant_domain`, `issue_type`, `issue_summary`, `desired_resolution`, `order_number`, `order_date`, `support_email`). `cases.service.set_fact` is the **only** writer of those columns, so a column always equals the latest fact for its key. `update_fields` accepts only operational fields (`gmail_thread_id`, `auto_reply_enabled`).
- `support_cases.version` is SQLAlchemy's `version_id_col`: every UPDATE bumps it, and a write based on a stale read raises `ConcurrentCaseUpdateError`. The caller rolls back and retries the unit of work.
- `focused` is a boolean guarded by a partial unique index (`user_id WHERE focused`, portable to SQLite), so each user has at most one focused case. `focus_case` moves focus, and closing a case clears it.
- The log tables (`case_facts`, `case_transitions`) use integer ids so their rows have a total order. Entity tables use UUIDs.
- Services take an `AsyncSession` and never commit. The caller owns the transaction (`Database.transaction()`).

### D9. Integer event ids (decided in M2)
- `events.id` is an autoincrementing integer, like the D8 log tables, not a UUID. The worker's per-user ordering needs a total order that doesn't depend on clock resolution.
- `case_transitions.event_id` became an integer FK to `events.id` in migration `0002`. It was a UUID placeholder in M1 that nothing wrote to.
- Ids can have gaps on Postgres (a deduplicated insert still uses a sequence value). Nothing relies on them being contiguous.

### D10. Outbound Telegram calls go through the queue (decided in M3)
- Handlers never call Telegram. They queue each call (send a message, acknowledge a button press, remove a message's buttons) through `TelegramOutbox`. Each call becomes a `TELEGRAM_OUTBOUND` event, written in the handler's transaction, and `TelegramDelivery` makes the call when the worker runs that event.
- Why: a Telegram outage retries only the delivery, never the handler and (from M5) its LLM calls. A message with buttons is sent only after its `pending_actions` rows are committed. Replies keep their order because a user's events run one at a time.
- Delivery is at-least-once. A crash between Telegram accepting a message and the commit resends it, which is acceptable for chat. Email has its own guard (D2).
- Messages are plain text (no `parse_mode`, so there is nothing to escape) and are split at Telegram's 4096 UTF-16-unit limit, with any buttons on the last part.
- Acknowledging a press and removing buttons are tidy-up calls. If Telegram rejects one ("query is too old", "message is not modified"), it is logged and the event completes. Network errors still retry.
- When an event goes `dead`, `TelegramDeadEventNotifier` queues a short notice to the user. A failed notice is never itself reported, so failures can't cascade.
- The bot talks only in private chats, where the chat id equals the user's Telegram id, so replies go to `users.telegram_user_id`.

### D11. Agent steps are structured decisions (decided in M4)
- `LLMClient` has two calls: `complete` (free text) and `extract_structured` (a validated Pydantic model). There is **no `run_agent`** and no native function calling.
- Each agent step (M5) is one `extract_structured` call returning an `AgentDecision` whose `action` is a tagged union of the permitted tool calls. Code validates the decision, enforces the tool's `ToolRiskLevel`, runs the tool, and feeds the result back as a delimited data block. The bounded loop lives in `agent/runtime.py`.
- Why: one schema per step, no provider-specific tool-call message plumbing, trivially faked in tests, and every action the model proposes is a typed object that code checks before anything happens.
- Output that still fails validation after the one repair retry raises `InvalidAgentDecisionError`, a `PermanentEventError`: the event goes `dead` and the user is notified. This caps an attempt at 2 calls, where worker retries could have made up to 16.
- Requests use the OpenAI Responses API with `store=False` and send the full context every time. The database owns conversation state (`previous_response_id` is never used).
- The SDK retries brief failures itself (`OPENAI_MAX_RETRIES`, default 1, with `OPENAI_TIMEOUT_SECONDS`, default 45). The worst case for one structured call (45s x 2 tries x 2 for the repair) stays under the handler cutoff.
- LLM-facing schemas stay within OpenAI strict mode (decided in M5): the action union is a plain `Union` of models tagged by a required `tool: Literal[...]` (a Pydantic discriminated union emits `oneOf`, which strict mode rejects); fields have no defaults; and limits such as maximum lengths are validators, not schema keywords, so a violation goes through the repair retry.

### D12. An intake turn is one decision carrying facts and an action (decided in M5)
- Each user message is one agent turn. The `IntakeDecision` holds both the facts stated in the message (`facts`) and the next action (`ask_user`, `reply_to_user` or `finish_intake`), so a typical turn costs **one** LLM call rather than an extraction call plus a question call.
- Recording facts is not a tool. It is our own state, written by code with provenance (`user_message`, `source_ref = telegram:<message_id>`). Tools are for talking to the user and, from M8, for lookups.
- The runtime hands every decision to a review hook **before** the action runs. The intake hook validates and records the facts, recomputes the missing requirements in code (`agent/policies.py`) and may replace the action: nothing missing means `finish_intake` (show the summary, move to `READY_TO_DRAFT`); the model finishing while something is missing means a code-written fallback question. So the model never decides readiness, and a question about something just answered is never sent.
- Facts are checked before they are recorded. Every fact carries a `quote` of the user's latest message, and a fact whose quote isn't in that message is dropped, so the model can normalize a value ("money back" → refund) but can't record a detail the user never mentioned. An order number must appear in the user's message, an order date must be ISO, not in the future, and quoted from words that say when (a number, month, weekday or relative day, not just "my order"), an unknown issue type becomes `other`, and an unchanged value is not appended again.
- An order is identified by an order number **or** an order date (user decision, M5). Relative dates resolve against `USER_TIMEZONE`.
- Cases open lazily: only when the decision records a fact or asks a question. Small talk opens no case.
- (M6) Every issue type also requires `support_email`, asked last. Like an order number, it is recorded only if it appears in the user's message, and it must look like an address. `signature_name` is an optional fact with the same check.

### D13. Drafting and approval (decided in M6)
- **Drafting is its own event.** When intake completes, it moves the case to `READY_TO_DRAFT` and queues a `DRAFT_EMAIL` event (deduplicated per causing event) in the same transaction. Two worst-case structured calls in one handler could exceed the handler cutoff, and a drafting failure should retry only the drafting. The handler does nothing unless the case is still `READY_TO_DRAFT`, so a duplicate is harmless. If a draft event dies, any later message to the `READY_TO_DRAFT` case queues it again.
- **The model writes the subject and body; code does the rest.** The recipient is the `support_email` fact. The sign-off (`Thank you,` plus a name) is added by code: the case's `signature_name` fact if the user gave one, else `users.signature_name` (nothing sets it yet), else the Telegram name (`users.display_name`, first and last name, refreshed at ingest). Drafts with placeholders or their own sign-off fail validation and get the repair retry. Each version stores `body_text` (as drafted) and `body` (as sent), so code can re-address or re-sign a draft without another model call.
- **The approval record is the consumed Send button.** Buttons under a draft carry `{outbound_email_id, content_hash}`. A Send press consumes the action (D3), then `drafts.approve` runs one conditional UPDATE (`status = awaiting_approval AND content_hash = <button's hash>`) and stores `approved_by_action_id`. `EmailApprovalVerifier` (for M7's `send_support_email`) re-checks that whole chain from the database and recomputes the hash from the stored content.
- **Text never approves.** A message while a draft waits goes to a draft-review turn whose actions are only `draft_support_email` and `reply_to_user`. Facts in the message are recorded as in intake. If a fact changed but the model only replied, the old Send button must not approve stale details. When only facts code fills in changed (`CODE_FILLED`: the recipient and signature), code re-issues the current text as a new version. When a fact the text itself states changed (order number, date, items, ...), the draft is superseded, the case goes back to `READY_TO_DRAFT`, and a `DRAFT_EMAIL` event rewrites it. The drafter sees the version it replaces (`previous_draft`), so the user's earlier edits carry over; the facts are authoritative. If a change leaves something required missing, the draft is superseded and the case goes back to `GATHERING_CONTEXT`.
- **Buttons.** A new version supersedes the old version's button group and removes it from the chat. If the waiting draft has no open buttons (after Edit then "never mind", or Cancel then "Keep it"), code shows it again with fresh buttons. Draft buttons don't expire; the hash binding is the guard.
- At most one email per case is live (`awaiting_approval`, `approved` or `sending`), enforced by a partial unique index.

### D14. The send claim commits before Gmail is called (decided in M7)
- D1 runs a handler in one transaction with the event's completion, so a `sending` claim written in that transaction isn't durable when Gmail is called. If the handler then fails after Gmail accepted the email (a crash, the handler timeout, a failed commit), the claim rolls back to `approved` and the retry sends the email again. This decision closes that gap.
- **Sending is its own `SEND_EMAIL` event**, queued by the Send press in the approval's transaction (deduplicated as `send:{outbound_email_id}`). It can't run in the button handler: that handler has already written the approval, and a second transaction can't claim a row the first one has written (SQLite: `database is locked`; Postgres: it waits on the row lock). Both were verified.
- **The send handler writes nothing in its own transaction before the claim.** It loads the email and case, has the executor verify the approval (`send_support_email` is `REQUIRES_APPROVAL`, checked by `EmailApprovalVerifier`), gets a Gmail access token and builds the message, all reads or retryable. Then it commits `approved → sending` in a separate short transaction (`EmailSender` gets the `Database` injected), calls Gmail, and records the outcome in its normal transaction.
- **After the claim, nothing leads to a second send:**
  - Gmail accepted it → `sent` with Gmail's message and thread ids; the case goes to `WAITING_FOR_SUPPORT` with `gmail_thread_id`, and the user is told what went out and to whom.
  - An error that proves Gmail didn't get it (`GmailPermanentError`: a 4xx refusal or bad credentials; `GmailUnreachableError`: the connection was never made) → `failed`; the case returns to `WAITING_FOR_USER_APPROVAL` and the same content is offered again as a new version, so trying again takes a new Send press.
  - Anything else (a timeout, a 5xx, an unexpected error) → `needs_attention`. The case stays `READY_TO_SEND` and the user is asked to check Gmail's Sent folder: **[It was sent]** marks it `sent` (Gmail's ids unknown until M8 can search by Message-ID) and moves on to `WAITING_FOR_SUPPORT`; **[It wasn't sent]** marks it `failed` and offers it again.
  - A handler that dies after the claim leaves the email `sending`. The retry finds it so and treats it as `needs_attention`; it never calls Gmail again.
- Before the claim, a temporary error (e.g. the token endpoint timing out) retries normally, with the email still `approved`. If the event gives up, the user's next message settles it: a user's events run in order, so a message handled while the case is `READY_TO_SEND` comes after the send event finished, and an email still `approved` then is marked `failed` and offered again.
- Each version gets its RFC 822 Message-ID when it is created (with a fixed domain; `make_msgid`'s default would expose the host name). The From display name is the email's signature name; the address comes from `GMAIL_SENDER_ADDRESS`, since the `gmail.send` scope can't read the account's profile.
- `/cancel` with an email still `sending` (a send that gave up after claiming) marks it `needs_attention`, cancels the case, and tells the user it may already have gone out.
- The pattern applies to any future external action that must happen at most once: commit the claim on its own, then act, then record.

### D15. Deployment (decided in M7.5)
- **Railway**, Hobby plan after the trial (the free plan's ~$1 monthly credit can't keep an always-on service up). One service built from the plain `Dockerfile`, plus Railway Postgres over the private network. `railway.json` holds the Railway-specific settings: pre-deploy `alembic upgrade head`, health check `/health`, restart on failure, no app sleeping, and `drainingSeconds: 30`. Everything else is environment variables, so moving to Render or Fly.io means one new config file.
- Compared: Fly.io (its managed Postgres starts around $38/mo; its defaults of stopping idle machines, 2 machines and a 5s kill timeout each work against D1) and Render (about $13/mo, with truly managed Postgres). Railway was the cheapest always-on option with the least to operate. Its Postgres is a container on a volume, so backups are the owner's job (README runbook).
- **One instance.** The worker is safe with several (D1), so a redeploy's brief overlap is harmless, but there is no reason to run more than one.
- **Migrations run before the new version takes traffic, while the old one still serves.** So every migration must be backward compatible with the previous release: add first, remove in a later release. A failed migration stops the deploy. Rollbacks redeploy old code but never downgrade the schema.
- **Shutdown.** Uvicorn is PID 1 (`exec` in the Dockerfile; under `sh -c` it never got SIGTERM, so the worker was killed without releasing its events). It gets 10s for open requests, then the worker grace; `drainingSeconds` must exceed the sum.
- **Fail fast on config.** With `ENVIRONMENT=production`, startup refuses a SQLite URL, a non-https `APP_BASE_URL`, or any missing Telegram, OpenAI or Gmail setting, naming the settings, not their values. A misconfigured deploy fails its health check instead of running with Gmail or the webhook silently off.
- **Database URL.** Hosts give `postgresql://` or `postgres://`; `Settings` rewrites it to `postgresql+asyncpg://` and `sslmode=` to asyncpg's `ssl=`, so the host's variable can be referenced directly.
- **The webhook is registered by hand** (`scripts/telegram_webhook.py set`, which checks `/health` first), not at app startup, so a local run with the production token can never repoint the bot. The local poller refuses to start while the bot has a webhook (`--take-over` overrides), because deleting it would send production's messages to the laptop's database. Local development uses a separate dev bot.
- **Logs** are structlog JSON in production; the uvicorn access log is off (health-check noise, and the app logs every event it handles). `/health` stays a liveness check without a database check, so a Postgres blip doesn't cause restart loops.

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
| `case_transitions` | M1 | Audit log: from, to, reason, actor, event_id (FK to `events`, M2), at (Invariant 8) |
| `case_messages` | M5 | Chat log for LLM context (last 20 per turn). Not workflow state. |
| `events` | M2 | Inbox and job queue (D1, D9): type, source, external_id, payload, status, attempts, run_at, lease |
| `pending_actions` | M3 | Button actions (D3) |
| `outbound_emails` | M6 | Drafts, approvals and sends (D2, D13, D14): one row per version; M7 adds the Message-ID and Gmail's ids; M8.5 adds `from_address` |
| `email_messages` | M10 | Inbound emails being tracked: unique `(account, gmail_message_id)`, thread, case, classification |
| `gmail_sync_state` | M10 | `history_id` and watch expiration per Gmail account (M8.5) |

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
- `events/models.py` (the `events` table, `EventType`, `EventSource`, `EventStatus`), `events/schemas.py` (`NewEvent` from adapters, `ClaimedEvent` for handlers), `events/service.py` (deduplicating `enqueue` that also schedules, `claim_next`, `complete`, `fail`, `release`, `recover_expired`), `events/worker.py` (lifespan task, per-user serialization, leases), and a handler registry in `events/handlers.py`.
- `scripts/inject_event.py` to push a synthetic event during local development.
- **Verify:** tests show that a duplicate `(source, external_id)` insert is a no-op, that a failing handler retries with backoff and then goes `dead`, and that two events for one user never run at the same time.

### M3. Telegram adapter
- `telegram/client.py` (sendMessage, editMessageText, answerCallbackQuery), `telegram/keyboards.py` (builds from `pending_actions`), `api/telegram.py` webhook.
- The webhook checks the `X-Telegram-Bot-Api-Secret-Token` header and `TELEGRAM_ALLOWED_USER_ID`. Unauthorized updates are dropped with a log entry that contains no message content.
- Local mode: `scripts/telegram_poll.py` long-polls `getUpdates` and feeds updates through **the same** normalization path.
- A temporary `USER_MESSAGE` handler echoes the message back and shows a test button.
- Built as: `telegram/ingest.py` (the shared normalization path), `telegram/delivery.py` (outbox and delivery, D10), `telegram/notifier.py` (dead-event notices), `actions/` (`pending_actions` model and service, migration `0003`), `chat/handlers.py` (echo and button handlers), `events/routing.py` (handler wiring). Non-text messages get "I can only read text messages for now." The real webhook is exercised in M7.5; locally only polling is used.
- **Verify:** a real bot echoes a message and handles a button press end to end in polling mode. Tests cover the auth rejection, duplicate updates, and stale-button handling.

### M4. LLM layer
- `llm/client.py`: the `LLMClient` protocol (`complete`, `extract_structured`; `run_agent` was dropped, see D11), an `OpenAIClient` implementation, and a `FakeLLMClient` for tests. Typed errors: `LLMTemporaryError`, `InvalidAgentDecisionError`.
- Every structured output is validated against its Pydantic schema. One repair retry is allowed, then it raises a typed error.
- Built as: `llm/client.py` (`Message`, the protocol, `UnconfiguredLLMClient`), `llm/structured.py` (provider-independent validation and repair; the repair prompt names error locations, not values), `llm/openai_client.py` (Responses API, strict JSON schema from the SDK's `to_strict_json_schema`, error mapping, `llm_call` log), `llm/errors.py`, `llm/schemas.py` (`ExtractedIssue`), `llm/factory.py` (`build_llm_client`). Permanent errors: `LLMAuthenticationError` (401/403, missing key), `LLMQuotaError` (429 `insufficient_quota`, or `credit_balance_exhausted` (added in M7)), `LLMRequestError` (other 4xx), `LLMRefusalError`, `InvalidAgentDecisionError`. The SDK sends requests with `httpx2`, so its tests use an `httpx2.MockTransport` instead of respx. Wiring the client into the app lifespan waits for M5, its first user.
- **Verify:** unit tests with the fake client. `scripts/llm_smoke.py` runs `ExtractedIssue` extraction against the real API.

### M5. Agent runtime and intake conversation
- `tools/registry.py`: a `Tool` definition (name, Pydantic args and result, `ToolRiskLevel`). The executor enforces the risk level: `REQUIRES_APPROVAL` tools need an approval record id.
- `agent/runtime.py`: a bounded loop (at most N tool steps per event). Each step is one `extract_structured` call returning an `AgentDecision` (D11). `agent/policies.py` holds the required fields for each issue type (for example, missing item needs merchant, order identifier, missing items and desired resolution). `agent/prompts.py` includes the untrusted-content policy and the helper that renders untrusted content as delimited data blocks.
- Build the `LLMClient` in the app lifespan (`build_llm_client`) and pass it to the handlers.
- Handler flow for `USER_MESSAGE`: route to a case (D7), extract or merge facts (provenance = `user_message`), compute missing fields in code, and ask one short question per turn.
- **Verify:** an integration test with the fake LLM covers the complaint, one follow-up question, the answer, and the case reaching `READY_TO_DRAFT` with facts sourced correctly.
- Built as (see D12):
  - `tools/registry.py`: `ToolRiskLevel`, `Tool`, `ToolRegistry`, `ToolContext`, and `ToolExecutor` with an `ApprovalVerifier` (deny-all until M6). `tools/chat_tools.py` holds `ask_user` and `reply_to_user` (`LOW_RISK_WRITE`, terminal), and `tools/errors.py` the typed errors, all permanent.
  - `agent/runtime.py`: `run_turn` with the review hook and `MAX_STEPS = 4`. `agent/schemas.py`: `IntakeDecision`, `FactUpdate`, `StopAction`. `agent/policies.py`: `IssueType`, `IntakeField`, `Requirement`, `REQUIRED`, `missing_requirements`, fallback questions. `agent/prompts.py`: the intake prompt, `render_data_block` and the context. `agent/intake.py`: `IntakeAgent`, fact validation and the summary.
  - `cases/routing.py`: M5 routing. A message goes to the focused case while it is `GATHERING_CONTEXT` or `READY_TO_DRAFT`. `READY_TO_DRAFT` still takes corrections, and a new requirement sends the case back to `GATHERING_CONTEXT`.
  - `cases/messages.py` and `CaseMessage`: the `case_messages` chat log. Migration `0004` also deletes leftover M3 `echo_test` buttons.
  - `chat/handlers.py`: `/start`, `/help` and `/cancel` are handled in code, with no LLM call. `/cancel` shows **[Yes, cancel] [Keep it]** (`ActionKind.CANCEL_CASE` / `KEEP_CASE`), bound to the case's current status. The M3 echo is gone.
  - Wiring: `build_llm_client` runs in the lifespan, and the client is closed on shutdown. A blank `OPENAI_API_KEY` now counts as unset instead of crashing startup. New setting `USER_TIMEZONE` (dependency `tzdata`). `scripts/llm_smoke.py --intake` checks the real API against the `IntakeDecision` schema.

### M6. Drafting and approval
- A `draft_support_email` tool (LOW_RISK_WRITE) creates an `outbound_emails` row. Telegram shows the draft with **[Send] [Edit] [Cancel]**.
- Edit: the user types changes, the LLM revises, and the result is a new draft version with a new approval requirement. Cancel moves the case to `CANCELLED` after the user confirms.
- Until M9, the support email address comes from the user (the bot asks for it). Emails are signed with the user's configured name.
- **Verify:** tests show that text like "yeah looks good" never approves anything (only the button does), that an edit invalidates the old Send button, and that a double-pressed Send creates only one approval.
- Built as (see D13):
  - `email/models.py` (`OutboundEmail`, `OutboundEmailStatus`, migration `0005`), `email/drafts.py` (`content_hash`, `compose_body`, `create_version`, `approve`, `discard_live`), `email/approvals.py` (`DraftButtonPayload`, `EmailApprovalVerifier`), `email/errors.py`.
  - `tools/email_tools.py`: `draft_support_email` (`LOW_RISK_WRITE`, terminal), `present_draft`, `retire_buttons`, `ensure_draft_buttons`, `signature_for`.
  - `agent/drafting.py`: `DraftingAgent.draft` (the `DRAFT_EMAIL` handler) and `.review` (draft-review turn, `DraftReviewDecision`), `request_draft`. `agent/facts.py`: fact checks and recording shared by intake and review (moved out of `intake.py`). Prompts: `draft_messages`, `review_messages`.
  - `cases/routing.py`: `route_message` returns a `Stage` (intake, draft review, approved). `chat/handlers.py`: `SEND_EMAIL`, `EDIT_DRAFT`, `CANCEL_DRAFT` buttons; cancelling a case cancels its unsent email.
  - Intake: `support_email` is a requirement for every issue type; `signature_name` is optional. The M5 summary is replaced by a short "drafting now" notice.
  - Users: `display_name` holds the Telegram first and last name, refreshed at ingest (`TelegramUser.full_name`).
  - Send in M6 stopped at `approved` / `READY_TO_SEND`. M7 queues the send from the Send handler and wires `EmailApprovalVerifier` into `send_support_email` (D14). The From header's display name follows the case's signature name, so a different-name order doesn't reveal the usual name.
  - `scripts/llm_smoke.py --draft` checks `DraftSupportEmail` against the real API; a unit test checks that all agent schemas fit OpenAI strict mode.

### M7. Gmail send — Phase 1 complete
- `email/gmail_client.py` (token refresh, `send_message`), `scripts/gmail_auth.py`, and the `send_support_email` tool (REQUIRES_APPROVAL) using the D2 send guard.
- Store `gmail_thread_id` and `gmail_message_id`, move the case to `WAITING_FOR_SUPPORT`, and confirm to the user in Telegram what was sent and to whom.
- **Verify:** a full integration test with fakes, including a duplicate approval event that produces one send, and Gmail accepting an email before the handler fails (it must not be sent again; D14). A manual run sends a real email to the user's own second address.
- **Part 1 (done), the send path against a fake Gmail (D14):**
  - `email/gmail_client.py`: the `GmailClient` protocol (`authorize`, `send`) and `UnconfiguredGmailClient`, which the app uses until part 2 (every send fails as "Gmail isn't connected" and nothing goes out). Typed errors in `email/errors.py`, with `provably_not_sent`.
  - `email/mime.py` (`compose_mime`); `email/drafts.py` gains `claim_for_sending`, `mark_sent`, `mark_failed`, `mark_needs_attention`, `confirm_sent`, `latest_version` and `get_email`. Migration `0006`: `rfc822_message_id`, `gmail_message_id`, `gmail_thread_id`, `sent_at` on `outbound_emails`.
  - `tools/send_tools.py`: `send_support_email` (REQUIRES_APPROVAL), `offer_again`, `ask_if_sent`. `email/sending.py`: `EmailSender` (the `SEND_EMAIL` handler), `resume_unfinished_send`, `confirm_sent` / `confirm_not_sent`.
  - `chat/handlers.py`: the Send press queues `SEND_EMAIL`; `ActionKind.CONFIRM_SENT` / `CONFIRM_NOT_SENT`; a message while `READY_TO_SEND` settles an unfinished send; `/cancel` handles an email still `sending`. New setting `GMAIL_SENDER_ADDRESS`.
  - Tests: `tests/integration/test_sending_flow.py` (SQLite and Postgres), with `FakeGmailClient`.
- **Part 2 (done):**
  - `email/gmail_client.py`: `HttpGmailClient` (refresh-token grant and `users.messages.send` over `httpx`, see D4) and `build_gmail_client`, which the lifespan uses; with any credential missing or blank it falls back to `UnconfiguredGmailClient`. The access token lives in memory only, is refreshed 5 minutes before expiry under a lock, and is dropped on a 401. A refreshed token without the `gmail.send` scope is refused.
  - Error mapping (D14). Token endpoint: 4xx → `GmailAuthenticationError`; 5xx, 429 or a timeout → `GmailTemporaryError` (retried before the claim); no connection → `GmailUnreachableError`. Send: no connection (`ConnectError`, `ConnectTimeout`, `PoolTimeout`), or a token failure inside `send` → `GmailUnreachableError` (nothing sent); 401, or a 403 about setup (`insufficientPermissions`, `accessNotConfigured`) → `GmailAuthenticationError`; any other 4xx, 429 included → `GmailRejectedError`; 5xx, a read timeout, a dropped connection, or a 2xx without Gmail's ids → `GmailTemporaryError` (outcome unknown, so `needs_attention`). Errors carry the call, the HTTP status and Google's short error code, never the message text, a token or an address.
  - `scripts/gmail_auth.py` (D6). Routing for a `WAITING_FOR_SUPPORT` case (D7): `Route.sent`, `SentCase` and the `sent_case` block in `agent/prompts.py`, and the guard in `agent/intake.py`.
  - Tests: `tests/unit/test_gmail_client.py` (respx), `tests/integration/test_gmail_send.py` (the real client on mocked HTTP through the Send flow), and the sent-case routing tests in `test_sending_flow.py`.
  - Verified 2026-09-28: a real email went from a Telegram chat, through the owner's Gmail, to their second address. It landed in that account's spam folder (a first contact about a merchant refund, between two personal accounts); the `@swordbot.invalid` Message-ID domain is a candidate to change to the sender's domain.

### M7.5. First deployment
- Pick a platform (see Open Decisions), set up a managed Postgres, set the Telegram webhook with a secret, and run migrations as a release step.
- **Verify:** the Phase 1 flow works from a phone with the laptop off.
- Built as (see D15):
  - `railway.json`; Dockerfile `CMD` uses `exec` and turns off the access log, with a 10s graceful HTTP shutdown.
  - `app/config.py`: `normalize_database_url`, `WORKER_SHUTDOWN_GRACE_SECONDS` (passed to `worker.stop`), `production_config_errors` / `check_production_config` (run first in the lifespan; raises `ConfigurationError`).
  - `app/telegram/webhook_setup.py` (`WEBHOOK_PATH`, `webhook_url`); `HttpTelegramClient.set_webhook` and `get_webhook_info` (the latter also on the protocol and `WebhookInfo` in schemas); `scripts/telegram_webhook.py info|set|delete`.
  - `run_polling` checks `getWebhookInfo` and raises `WebhookActiveError` unless `take_over`; `scripts/telegram_poll.py --take-over`.
  - Tests: `tests/unit/test_config.py`, `tests/unit/test_telegram_webhook_setup.py`, new client tests, polling guard tests. The image was checked locally: the pre-deploy migration against a `postgresql://` URL, startup refusal with missing settings, the webhook's 401/200, and a clean SIGTERM shutdown.
  - The runbook (resources, variables, webhook, dev bot, spending limit, backups) and the verification checklist are in the README's Deployment section.

---

## Phase 2 — Context gathering and inbound email

### M8. Gmail receipt search
- Add the `gmail.readonly` scope. Update the privacy policy linked from the OAuth app's Branding page (`site/privacy.html`, published by `.github/workflows/pages.yml`) to cover reading receipts **before** requesting the scope. Tools: `search_order_emails(merchant, approximate_date, order_number?)` returns `OrderEmailCandidate`s, and `read_email(message_id)`.
- Receipts are parsed in code first (HTML to text, then trimmed). Only the trimmed receipt text goes to `extract_structured` to produce `ReceiptInfo` (order number, date, total, items, and any support contact).
- Before any fact from a receipt is used, the bot asks "I found order #… for $… — is this it? [Yes] [No]". Provenance = `gmail_receipt`.
- **Verify:** fixture emails (DoorDash, Amazon, generic) extract correctly. The LLM only ever receives the minimal content.
- Build the search against one `GmailClient` passed in as a parameter (not a global), so M8.5 can run it once per account.

### M8.5. Multiple Gmail accounts
The user shops from more than one Gmail account. Support finds an order by the address it was placed with, so each case must send from, search in, and follow the replies of the right account. Until this milestone, everything uses the one account from M7. Gmail and Google Workspace accounts only: other providers (Outlook, iCloud, ...) would each need their own adapter and are not planned.

- **Accounts.** All accounts share the one OAuth client, and each gets its own refresh token from `scripts/gmail_auth.py`, run once per account. The script also requests the `openid email` scopes, so it can show which account actually consented and refuse a token for an unexpected address. The tokens stay in env/host secrets as `GMAIL_ACCOUNTS`, a list of `{address, refresh_token}` with one marked default. D6's encrypted table is for multiple *users*; this is still one user. The M7 variables (`GMAIL_REFRESH_TOKEN`, `GMAIL_SENDER_ADDRESS`) keep working as a one-account list. `--check` checks every account.
- **Clients.** `build_gmail_clients` returns one `HttpGmailClient` per address (`GmailAccounts`: lookup by address, plus the default). An account with bad credentials is reported per account and doesn't stop the others.
- **Which account a case uses.** It is a case fact, `gmail_account`, and it must be one of the configured addresses:
  1. One account configured: that account, with no question.
  2. Otherwise, **search all accounts** for the order (M8's search, run concurrently, one call per account). The M8 confirmation names the account ("I found order #… for $… in you@work.com. Is this it? [Yes] [No]"). A confirmed receipt sets the account, with provenance `gmail_receipt`.
  3. If the order turns up in no account, in more than one, or the user says No to every candidate: **ask with buttons**, one per configured account, default first. Never free text. If the user names the account in a message ("it was on my work email"), it is recorded only if it matches a configured address; anything else gets the buttons.
  4. The account is a requirement before drafting, like `support_email` (D12).
  - A mailbox that can't be searched (revoked token, outage) is skipped. The user is told which one, and the account can still be picked with the buttons.
- **Approval covers the From address.** `content_hash` adds the sending address, and the draft preview shows `From:` next to `To:`. Changing the account is a code-filled change (D13): a new version and a new Send press, so a draft approved for one account never goes out from another. `outbound_emails.from_address` stores it. A migration adds the column, filling in the default account, and drafts awaiting approval at upgrade are re-issued as new versions, since their stored hash no longer matches.
- **Sending.** `EmailSender` uses the client for `email.from_address`, never silently the default. From = that address, with the case's signature name. Messages name the account ("Gmail isn't connected for you@work.com"; "check the Sent folder of you@work.com").
- **Threads.** Gmail thread and message ids are only unique within one mailbox. `support_cases` gets `gmail_account` alongside `gmail_thread_id`, and everything that matches on them (M10) matches on the pair.
- **Verify:** tests show that
  - an order found only in the second account sets the account, and the email goes out through that account's client;
  - no match, or matches in two accounts, asks with buttons;
  - a text answer naming an unknown address isn't recorded;
  - switching the account invalidates the old Send button;
  - one revoked account doesn't stop the search, and the user is told;
  - the M7 single-account variables still work.

  Manual check: a real send from a second account, and `gmail_auth.py --check` with two accounts.

### M9. Support contact discovery
- `web/search.py` defines a `WebSearchClient` protocol and one provider (see Open Decisions). `web/support_discovery.py` applies the priority order: receipt, then official domain, then official help center, then search results on official domains, then a fallback.
- An official domain is one that matches `merchant_domain`, which comes from the receipt sender or the user. Store the contact with its method, address, source URL, source type and confidence. If no email channel exists, tell the user which channel the merchant requires.
- **Verify:** unit tests with canned search results check that a third-party page's address is ranked below the official domain.

### M10. Inbound email tracking
- `api/gmail.py` is a Pub/Sub push endpoint that verifies the Google OIDC JWT. It runs `history.list` from the stored `history_id` and turns each new message into an `EMAIL_RECEIVED` event (deduped on `(account, gmail_message_id)`).
- A `users.watch` renewal is scheduled as a recurring event every 6 days (watches expire after 7). There is also a local-dev polling fallback. Watches, `history_id` and renewals are **per account** (M8.5): the push notification names the mailbox, and each account has its own sync state.
- `email/threading.py` matches by `(account, thread id)` first, then falls back to In-Reply-To/References. Messages sent by the user, from any configured account, are ignored.
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
2. ~~**Hosting platform (M7.5)**~~ Decided: Railway, trial first, then the Hobby plan. See D15.
3. **Web search provider (M9):** OpenAI's built-in web search tool, Brave Search API, or Tavily. This affects cost and adds another API key.
4. ~~**Signature/display name** used in emails (M6)~~ Decided: the Telegram name by default, overridable per case (for orders placed under another name). See D13.
