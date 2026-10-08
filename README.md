# swordbot

A personal customer-support assistant. You describe an order problem to a Telegram bot ("My DoorDash order was missing the fries"). The bot works out the details, drafts an email to the merchant's support team, and sends it from your Gmail only after you press **Send**. When support replies, the bot picks the case back up. It asks you before making any consequential decision, such as accepting store credit instead of a refund.

> Status: early development. The scaffold, the case domain (database, state machine, facts with provenance), the event queue with its worker, the Telegram adapter, the LLM client layer, the intake conversation, and drafting with **[Send] [Edit] [Cancel]** approval exist. Tell the bot about an order problem, answer its questions, and it shows you the email it would send. Press Send and it goes out from your Gmail (see [Gmail setup](#gmail-setup)). Phase 1 is complete, and the bot deploys to Railway (see [Deployment](#deployment)). Phase 2 has started: the bot now looks up the order's receipt in your Gmail instead of asking for the order number (see [Finding the order in Gmail](#finding-the-order-in-gmail)). Replies aren't read yet, so they arrive only in your inbox. See [docs/PLAN.md](docs/PLAN.md) for the roadmap.

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

Main tables so far: `users`, `support_cases` (status, fact-backed fields, optimistic-lock `version`), `case_facts` (append-only facts with `source` provenance), `case_transitions` (audit log of every status change), `events` (the inbox and job queue, below), `pending_actions` (the records behind chat buttons), `case_messages` (the chat about each case, kept as LLM context; not workflow state) and `outbound_emails` (one row per email version, with its approval bound to a hash of its content). The allowed status transitions are listed in [docs/PLAN.md](docs/PLAN.md#case-state-machine).

## Events and the worker

Every input becomes a row in `events` first: Telegram updates and Gmail notifications (from M3 and M10), and scheduled work such as follow-ups. A worker inside the app process then handles each one ([app/events/](app/events/), PLAN D1):

- **Duplicates are dropped.** `(source, external_id)` is unique, so a redelivered Telegram update or Gmail notification is ignored.
- **One at a time per user, in order.** A user's events never run concurrently, and a newer event waits while an older one is retrying. Different users' events run in parallel on Postgres.
- **Retries.** A failing event is retried with backoff (15s, 30s, 1m, … capped at 30m) for 8 attempts, about 30 minutes, and is then marked `dead` and reported. Errors that can never succeed on retry (`PermanentEventError`, such as an invalid payload) go straight to `dead`.
- **Crash safety.** A claimed event holds a 5-minute lease. If the process dies, the event is retried once the lease expires. A handler's database writes commit only together with the event being marked done. On a normal shutdown (such as a redeploy), in-flight events get `WORKER_SHUTDOWN_GRACE_SECONDS` to finish and are then handed back without counting the attempt.
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
| `USER_TIMEZONE` | Your IANA timezone, such as `America/Los_Angeles` (default `UTC`). Turns "tonight" or "yesterday" into the right order date |
| `APP_BASE_URL` | Public HTTPS URL of the deployment, used to register the Telegram webhook. Required in production |
| `DATABASE_URL` | Postgres URL. `postgresql://` and `postgres://` (as hosts hand them out) become `postgresql+asyncpg://`, and `sslmode=` becomes asyncpg's `ssl=`. `sqlite+aiosqlite:///…` for local development only |
| `TEST_DATABASE_URL` | Tests only. A disposable Postgres database; when set, DB tests also run against Postgres |
| `WORKER_ENABLED` | Run the event worker in the app process (default `true`) |
| `WORKER_CONCURRENCY` | Events processed at once, across different users (default `4`; always 1 on SQLite) |
| `WORKER_POLL_INTERVAL_SECONDS` | How often the worker checks for due events (default `1.0`). Only retries, scheduled events and events queued by another process (such as `scripts/telegram_poll.py`) wait for it; webhook messages wake the worker at once. Railway uses `15`, matching the first retry delay; keep `1.0` locally when polling |
| `EVENT_MAX_ATTEMPTS` | Attempts before an event is marked `dead` (default `8`) |
| `EVENT_RETRY_BASE_SECONDS`, `EVENT_RETRY_MAX_SECONDS` | Exponential backoff start and cap (defaults `15` and `1800`) |
| `EVENT_LEASE_SECONDS` | How long a claimed event may run before it is presumed crashed (default `300`) |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | On shutdown, how long in-flight events may finish before they are handed back (default `10`). The host's stop timeout must be longer |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | LLM access and model name (default `gpt-5`) |
| `OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_RETRIES` | Per-request timeout and the SDK's own quick retries (defaults `45` and `1`) |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | Secret that Telegram echoes back in `X-Telegram-Bot-Api-Secret-Token` |
| `TELEGRAM_ALLOWED_USER_ID` | The only Telegram user the bot responds to (your numeric id) |
| `TELEGRAM_API_BASE_URL` | Bot API base URL (default `https://api.telegram.org`) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Your "Desktop app" OAuth client (see [Gmail setup](#gmail-setup)) |
| `GOOGLE_REDIRECT_URI` | Loopback address `scripts/gmail_auth.py` listens on during consent (default `http://127.0.0.1:8080/`). Only the script uses it |
| `GMAIL_REFRESH_TOKEN` | Printed by `scripts/gmail_auth.py`. Without all three Gmail credentials, every send reports "Gmail isn't connected" and nothing goes out. A token without the `gmail.readonly` scope (one from before M8) still sends; only receipt lookup is off |
| `GMAIL_SENDER_ADDRESS` | Your Gmail address, for the From header (the `gmail.send` scope can't read it). The display name follows each case's signature name. Without it, Gmail fills in From itself |

With `ENVIRONMENT=production` (the Docker image's default) the app refuses to start unless `DATABASE_URL` is Postgres, `APP_BASE_URL` is `https://`, and every Telegram, OpenAI and Gmail setting above is set. The error names the missing settings, never their values.

Secrets are loaded as `SecretStr` and are never logged. Log output also passes through a redaction step (`app/logging.py`).

## LLM

All model calls go through `LLMClient` ([app/llm/](app/llm/), PLAN D4 and D11), on the OpenAI Responses API. Only `app/llm/openai_client.py` imports the `openai` SDK.

- **Two calls.** `complete` returns free text. `extract_structured` returns a Pydantic model: the request carries a strict JSON schema, the reply is validated in code, and invalid output gets **one** repair retry. If it is still invalid, the event fails with `InvalidAgentDecisionError` and you get a Telegram notice. Agent steps are structured outputs too (see Intake conversation).
- **Stateless, not stored.** Every request sends the full context and sets `store=false`, so OpenAI keeps no stored response to chain from; the database owns conversation state. Only the minimum content needed for the task is sent.
- **Errors.** Timeouts, connection errors, 5xx and rate limits are temporary and retried by the worker. A bad key (401/403), no credit left (`insufficient_quota` or `credit_balance_exhausted`), a rejected request (such as an unknown model) or a refusal is permanent. Error messages carry only the status and OpenAI's error code.
- **Logs.** Each call logs an `llm_call` line with purpose, model, schema, attempts, duration and token usage, never prompts or output.

To check your key and model against the real API (one or two small calls):

```bash
uv run python scripts/llm_smoke.py
uv run python scripts/llm_smoke.py --text "Amazon sent me the wrong charger"
uv run python scripts/llm_smoke.py --intake   # the real intake prompt and IntakeDecision schema
uv run python scripts/llm_smoke.py --draft    # a draft for a sample case (DraftSupportEmail)
uv run python scripts/llm_smoke.py --receipt  # reading a sample order email (ReceiptInfo)
```

It prints the extracted `ExtractedIssue` (or, with `--intake`, the `IntakeDecision`; with `--draft`, the drafted subject and body; with `--receipt`, the `ReceiptInfo`) as JSON, or a typed error such as `LLMAuthenticationError: 401: invalid_api_key`.

## Intake conversation

Tell the bot what went wrong in plain words. It opens a **case**, records each detail you give as a **fact** tagged with its source (your Telegram message), and asks one short question per turn until it has what it needs. Then the case is **ready to draft**, and the bot drafts the email straight away (see below).

What "what it needs" means is decided in code ([app/agent/policies.py](app/agent/policies.py)), not by the model. Every case needs the kind of problem, a short description, the merchant, what you want done (refund, replacement, ...) and the merchant's **support email address**, which you type in until address lookup arrives (M9). Order problems also need the order (an order number **or** the date you ordered), and for missing, wrong or damaged items, which items. The bot looks the order up in your Gmail before asking you for it (next section).

- **One model call per message.** The model returns the facts it found in your message and a proposed next step. Code records the facts, recomputes what's missing and decides: the model's question, a fallback question of its own, or "drafting now". Only code moves a case between "gathering context" and "ready to draft".
- **No invented details.** Facts are only what you said: the model must quote your words for every detail it records, and a detail whose quote isn't in your message is dropped. An order date needs words that say when ("last night", "on the 23rd"). An order number, support address or name the model reports is dropped unless it appears in your message, a support address must look like one, and an order date must be a real date that isn't in the future.
- **Corrections.** Send a correction any time ("actually it was order #A123"). The new value is recorded alongside the old one.
- **Small talk** ("hi", "thanks") gets a short reply and opens no case.
- **Commands.** `/help` (or `/start`) explains the bot. `/cancel` offers to cancel the case you're working on, with **[Yes, cancel] [Keep it]** buttons. Only the button cancels, and nothing is sent to support.

Under the hood ([app/agent/](app/agent/), [app/tools/](app/tools/), PLAN D11 and D12): each step of the agent is one structured LLM call whose action is one of the tools it's allowed to use. The tool executor enforces each tool's risk level in code. `read_only` and `low_risk_write` tools (so far `ask_user` and `reply_to_user`, which message you, `draft_support_email`, which saves a draft and shows it to you, and `search_order_emails` and `read_email`, which look in your mailbox) can run. `requires_approval` tools need an approval record, and the agent can never supply one: approvals come only from your button presses. The loop is capped at 4 steps per message. The last 20 messages of the case are sent as context. Case facts and tool results go into delimited data blocks, and the prompt tells the model to treat them as data, never as instructions.

## Finding the order in Gmail

Once the bot knows the merchant, and you haven't typed an order number, it looks for the order's receipt in your Gmail before asking anything more. So when it can search, the first thing it asks for after the problem itself is the store, never the order number ([app/agent/receipts.py](app/agent/receipts.py), PLAN D16). You see the result, not the search:

```text
I found this order in your Gmail:

Order from DoorDash
Order number: DD-48213
Date: Sep 23, 2026
Total: $32.81
Items: Cheeseburger, Garlic Fries, Vanilla Shake

From the email "Order Confirmation for Roy from Burger Palace"

Is this the order you mean?
[Yes] [No]
```

- **Yes** records the order number, date, total and items as facts, each sourced to that email (`gmail_receipt`, with its Gmail message id). **No** shows the next-best match, if there is one; after that the bot goes back to asking you. Typing "yes" does nothing: only the buttons count, and nothing from an email is recorded before you press one.
- **A support address in the receipt is asked about separately** (**[Use it] [No]**), and only if you haven't given one. A `no-reply` address is never offered.
- **What is searched.** One Gmail query: the merchant's name, receipt words (order, receipt, confirmation, invoice, total), and the days around the order date (or the last 30 days if you gave none), leaving out your Sent mail and the Promotions tab. Code reads up to 10 results and ranks them by how much they look like that merchant's receipt (sender, subject, an order number, a total, the date). Only the best 3 can ever reach the model.
- **What the model sees.** One email per call, already reduced in code to its visible text: no markup, scripts, links or hidden text, no attachments, at most 6,000 characters. It goes in a delimited data block and the prompt treats it as data, never instructions. The model's answer is then checked against that text: an order number, total, item or address that isn't literally in the email is dropped, and an order date must fit the email's date. So an email can't make the bot record something it doesn't say.
- **What is kept.** The checked details and the email's Gmail id. The email's text is not stored and never logged.
- **If the lookup can't run** (Gmail down, read access revoked, or a refresh token from before M8 without the `gmail.readonly` scope), nothing breaks: the bot asks you for the order as before.
- **Cost.** One model call per email read: usually one per case, three at most.

The same read access settles a send whose outcome is unknown (see [Sending](#sending)): the bot looks for the email in Gmail by its Message-ID before asking you.

## Drafting and approval

Once intake has everything, the bot writes the email to the merchant's support and shows it to you:

```text
Here's the email I'd send. Nothing goes out until you tap Send.

To: support@doordash.com
Subject: Missing fries from order #A123
...
Thank you,
Your Name

[Send] [Edit] [Cancel]
```

- **The model writes the subject and body; code does the rest.** The recipient is the support address you gave, and the sign-off is added by code: by default your Telegram name (first and last, kept up to date from your messages). If an order is under another name, say so ("the order is under Alex Kim, sign it with that") and that name signs this case's emails only. Drafts with placeholders like `[Your Name]` are rejected and the model gets one chance to fix them.
- **Send** approves exactly the email on screen. The approval is bound to a hash of the recipient, subject and body, so it can't carry over to anything else. Pressing Send twice approves once. Approving queues the send (see "Sending" below).
- **Edit**, or simply typing a change ("make it shorter", "ask for a replacement instead"), makes the bot write a new version with new buttons. The old version's buttons stop working. A detail you change here (a new support address, order number or name) is recorded as a fact too, and the email is always rewritten to match: a new address or name just re-addresses or re-signs it, while any other detail gets the text rewritten, keeping your earlier edits. If the change needs more information (say, the fries were crushed, not missing), the case goes back to intake for it.
- **Typing approval does nothing.** "Looks good, send it" gets a reply telling you to tap Send. The draft-review step can only write a new version or reply; approving isn't among its options, and only the Send button reaches the approval code.
- **Cancel** asks for confirmation (**[Yes, cancel] [Keep it]**). Cancelling the case also cancels its draft. "Keep it" brings the draft back with working buttons.

Every version is kept in `outbound_emails` with its status (`awaiting_approval`, `approved`, `sending`, `sent`, `failed`, `needs_attention`, `superseded`, `cancelled`), who approved it and when (PLAN D2 and D13).

## Sending

After you press **Send**, the email goes out from your Gmail as its own step, and the bot tells you what was sent and to whom. The case then waits for support's reply. Without Gmail credentials (see [Gmail setup](#gmail-setup)) every send reports "Gmail isn't connected", nothing goes out, and the email is offered again.

A send never happens twice (PLAN D14). Before calling Gmail, the bot commits a claim on the email (`approved → sending`) in its own transaction. After that:

- **Gmail accepted it:** the email is `sent`, with Gmail's message and thread ids.
- **It certainly didn't go out** (Gmail refused it, access was revoked, or Gmail couldn't be reached): the email is `failed` and shown again with fresh buttons. Trying again takes another Send press.
- **Anything else** (a timeout, a server error, a crash after Gmail may have accepted it): the bot first looks for the email in your Gmail by the Message-ID it gave it (needs the `gmail.readonly` scope). If it is there, the email is `sent` and you are told so. If it isn't found, that proves nothing (Gmail's search can lag), so the email is `needs_attention` and the bot asks you to check Gmail's Sent folder: **[It was sent]** or **[It wasn't sent]**. It never retries on its own.

If a send gives up before reaching Gmail (say, Gmail was unreachable for half an hour), your next message gets the email offered again. `/cancel` while a send's outcome is unknown cancels the case and warns you the email may already have gone out.

While a case waits for support, a message about it ("any news?") gets a short status reply and opens nothing. The bot can't read replies until M10, so they arrive only in your Gmail inbox. A different problem starts a new case, which then becomes the one the bot is working on.

## Gmail setup

The bot uses your own Gmail account through the Gmail API, with a refresh token you create once. It asks for two scopes:

| Scope | Since | What it allows | What the bot does with it |
|---|---|---|---|
| `https://www.googleapis.com/auth/gmail.send` | M7 | Send email as you | Sends the emails you approve with the Send button |
| `https://www.googleapis.com/auth/gmail.readonly` | M8 | Search and read your mailbox. It can't change or delete anything | Finds an order's receipt ([above](#finding-the-order-in-gmail)), and checks whether an email it sent went out. From M10, reads support's replies |

`gmail.readonly` is one of Google's "restricted" scopes. For a personal, unverified app that changes nothing except the wording of the consent warning. The privacy policy linked from the consent screen ([site/privacy.html](site/privacy.html)) describes exactly this use; keep it accurate when the use changes.

**Upgrading a token from before M8.** The old token keeps sending; receipt lookup stays off until you replace it. Run step 4 again (tick both permissions), put the new `GMAIL_REFRESH_TOKEN` in `.env` and in your host's variables, and check it with `--check`. The deploy and the token swap can happen in either order.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project (or pick one), open **APIs & Services → Library**, and enable the **Gmail API**.
2. Set up the **OAuth consent screen** (Google Auth Platform): user type **External**, any app name, your address as the contact. Google only publishes an app whose **Branding** has a home page URL and a privacy policy URL, with their domain under **Authorized domains**. This repo publishes both from [site/](site/) to GitHub Pages (`.github/workflows/pages.yml`; enable it once under **Settings → Pages → Source: GitHub Actions**): home page `https://royzhou-dev.github.io/swordbot/`, privacy policy `https://royzhou-dev.github.io/swordbot/privacy.html`, authorized domain `royzhou-dev.github.io`. Leave the logo empty, since a logo makes Google require verification. Then, under **Audience**, press **Publish app** so its status is **In production**. This matters: in **Testing**, Google expires refresh tokens after 7 days and sends start failing with "Gmail isn't connected". The app stays unverified, which is fine for personal use. Google shows a "Google hasn't verified this app" warning during consent; continue through **Advanced**.
3. Under **Credentials**, create an **OAuth client ID** of type **Desktop app**. Put its id and secret in `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
4. Run the consent flow and allow both permissions (sending, and reading):

   ```bash
   uv run python scripts/gmail_auth.py          # opens the browser; --no-browser prints the URL
   ```

   Sign in with the account the bot should use. The script listens on `GOOGLE_REDIRECT_URI` (default `http://127.0.0.1:8080/`) for Google's redirect, exchanges the code (with PKCE), and prints `GMAIL_REFRESH_TOKEN=...`. Put that line in `.env`, or in your host's secrets in production. It is as sensitive as a password: it can read your mail and send as you.
5. Set `GMAIL_SENDER_ADDRESS` to the same address, then check the setup. This sends and reads nothing, and says which of the two scopes the token has:

   ```bash
   uv run python scripts/gmail_auth.py --check
   ```

6. Restart the app. For a first real test, give the bot a second address of your own as the support email.

To disconnect, remove the app at [myaccount.google.com/permissions](https://myaccount.google.com/permissions). Sends then fail as "Gmail isn't connected" and nothing goes out; receipt lookup just stops, and the bot asks you for the order instead. Tokens are refreshed with plain HTTPS calls to Google's token endpoint (`app/email/gmail_client.py`); access tokens are kept only in memory and never logged.

## Telegram bot setup

The bot is its own Telegram account, created with @BotFather. You talk to it from your normal account. It can only see messages sent to it directly, and it answers only `TELEGRAM_ALLOWED_USER_ID`; everyone else is ignored without a reply. Chats with bots are not end-to-end encrypted, so order details and email drafts pass through Telegram's servers.

1. In Telegram, message **@BotFather**, send `/newbot`, and pick a display name and a username ending in `bot`. Put the token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send @BotFather `/setjoingroups`, pick your bot, and choose **Disable**, so nobody can add it to a group. The bot ignores group chats anyway.
3. Open your bot's chat and press **Start**. A bot can't message you until you do.
4. Find your numeric Telegram user id: run the poller below with `TELEGRAM_ALLOWED_USER_ID` empty and message the bot. The rejection log line shows `sender_id` (never the message). Put it in `TELEGRAM_ALLOWED_USER_ID`.
5. Set `TELEGRAM_WEBHOOK_SECRET` to a random string of letters, digits, `_` and `-` (webhook mode only, see [Deployment](#deployment)): `py -3.14 -c "import secrets; print(secrets.token_urlsafe(32))"`.

If the token ever leaks, send @BotFather `/revoke` and update `.env`.

**Local development uses polling.** Telegram can only deliver webhooks to a public HTTPS URL, so locally a second process fetches updates:

```bash
uv run uvicorn app.main:app --reload     # terminal 1: API + worker
uv run python scripts/telegram_poll.py   # terminal 2: Telegram -> events
```

The poller passes each update through the same code as the webhook. **Use a separate dev bot locally** once the bot is deployed: Telegram refuses polling while a webhook is set, and removing the webhook would send the deployed bot's messages to your laptop's database. So the poller stops with an error if the bot has a webhook. `--take-over` removes it anyway (pending updates are kept); `scripts/telegram_webhook.py set` gives the bot back to the deployment. Send the bot a complaint, such as "My DoorDash order tonight was missing the fries", and answer its questions. Try `/cancel` to see the confirmation buttons: a button works once, and pressing another button on the same message afterwards says it is no longer valid.

**Production uses the webhook** `POST /telegram/webhook`, registered by hand with `scripts/telegram_webhook.py set` (see [Deployment](#deployment)). It is disabled (404) unless `TELEGRAM_WEBHOOK_SECRET` is set, and rejects requests whose `X-Telegram-Bot-Api-Secret-Token` header doesn't match (401).

How it works ([app/telegram/](app/telegram/), PLAN D1, D3 and D10):

- **Inbound.** Each update is authorized (the allowed user, in a private chat) and stored as a `user_message` or `user_button_action` event, deduplicated on the message or button-press id. Updates that aren't messages or button presses are dropped.
- **Outbound.** Handlers never call Telegram directly. Each reply, button acknowledgement or button removal is queued as a `telegram_outbound` event in the handler's transaction and delivered by the worker. A Telegram outage retries only the delivery. If a message can't be delivered at all, you get a short notice once Telegram works again.
- **Buttons.** Each button is a `pending_actions` row; the button carries only its id. A press is accepted once, only from you, only before it expires and only while its case is still in the expected status. Pressing one button closes the others shown with it.
- **Secrets.** The bot token is part of every Bot API URL, so the `httpx` request logger is kept at WARNING, Telegram errors never include the URL, and anything shaped like a bot token is scrubbed from log values.

## Deployment

The bot runs on [Railway](https://railway.com) as one always-on container plus a Railway Postgres database (PLAN, open decision 2). Nothing in the repository is Railway-specific: the image is the plain [Dockerfile](Dockerfile), configured entirely by environment variables, and the few Railway settings are entered in its dashboard (runbook steps 4 and 5). Any host that runs a long-lived container works.

There is no `railway.json`. Railway deprecated Config as Code (new services can't opt in since 2026-08-28, and existing files stop working on 2026-12-01). Its replacement, Infrastructure as Code (`.railway/railway.ts`, applied with the Railway CLI), is more machinery than one service needs.

**What the setup relies on:**

- **Always on, exactly one instance.** The event worker runs inside the web process (PLAN D1), so the service must never sleep (serverless / app sleeping off) and runs one replica. During a redeploy the old and new containers briefly overlap. That is safe, because events are claimed with `SKIP LOCKED` and at most one runs per user.
- **Migrations are a pre-deploy step.** `alembic upgrade head` runs in the new image before it takes traffic, never at app startup. If it fails, the deploy stops and the old version keeps running. The old version is still serving while a migration runs, so **migrations must be backward compatible**: add columns and tables first, and remove them only in a later release.
- **Graceful shutdown.** Railway sends SIGTERM and waits `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` (30; Railway's default is only 3) before killing the container. Uvicorn is PID 1 (`exec` in the Dockerfile), so it gets the signal: it finishes open requests (up to 10s), then the worker gives in-flight events `WORKER_SHUTDOWN_GRACE_SECONDS` (10) and hands the rest back to run again. An email send cut off after its claim is never repeated; the bot asks you to check Gmail's Sent folder (PLAN D14).
- **Health check.** Railway waits for `GET /health` to answer before switching traffic. It is a liveness check only, with no database check, so a Postgres blip doesn't cause restart loops; the worker backs off on its own. A missing production setting makes startup fail (see [Environment variables](#environment-variables)), so a misconfigured deploy never goes live.
- **Logs** are JSON (`ENVIRONMENT=production` is the image default), which Railway's log view parses and filters by `level`, `event_id` or `case_id`. Uvicorn's access log is off.
- **Secrets** are Railway service variables. Seal each secret (variable menu → **Seal**): a sealed value is passed to the app but never shown again in the dashboard or the CLI.
- **Cost.** The trial's one-time $5 credit covers setup and testing. Running 24/7 needs the Hobby plan ($5 a month including $5 of usage). The free plan's roughly $1 of monthly credit can't keep the service up all month, and when credit runs out Railway stops the service: the bot goes silent, and Telegram drops messages it can't deliver within about a day.

### First deployment (runbook)

You do these steps yourself. They create billable resources and handle production secrets.

**Before you start:** push the repository to GitHub, and **stop the local app and poller**. The production bot is the one you already use; step 8 gives local development its own bot.

1. **Project and database.** At railway.com, sign in with GitHub, then **New Project → Deploy PostgreSQL**. This creates a service named `Postgres`.
2. **App service.** In the same project, **+ Create → GitHub Repo →** your `swordbot` repository. Railway finds the `Dockerfile` at the repository root and builds it. The first deploy fails because nothing is configured yet. That's expected.
3. **Public URL.** App service → **Settings → Networking → Generate Domain**, target port **8000**. You get a `https://<name>.up.railway.app` URL.
4. **Variables.** App service → **Variables → Raw Editor**, paste the block below and fill it in. The `${{…}}` references are resolved by Railway, so leave them as they are. Use your **production** bot's token, and generate a **new** webhook secret (`py -3.14 -c "import secrets; print(secrets.token_urlsafe(32))"`) and keep it for step 7. Afterwards, seal every secret: the bot token, webhook secret, OpenAI key, Google client secret and Gmail refresh token.

   ```text
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   APP_BASE_URL=https://${{RAILWAY_PUBLIC_DOMAIN}}
   PORT=8000
   USER_TIMEZONE=America/Los_Angeles
   LOG_LEVEL=INFO
   WORKER_POLL_INTERVAL_SECONDS=15
   RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30
   OPENAI_API_KEY=
   OPENAI_MODEL=
   TELEGRAM_BOT_TOKEN=
   TELEGRAM_WEBHOOK_SECRET=
   TELEGRAM_ALLOWED_USER_ID=
   GOOGLE_CLIENT_ID=
   GOOGLE_CLIENT_SECRET=
   GMAIL_REFRESH_TOKEN=
   GMAIL_SENDER_ADDRESS=
   ```

   The Gmail refresh token and OpenAI key can be the same ones as in your local `.env`. A 15-second worker poll is enough in production, because webhook messages wake the worker immediately; the poll only picks up retries and scheduled work. `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` is read by Railway, not the app: it gives the old container 30 seconds to shut down gracefully.
5. **Deploy settings.** App service → **Settings → Deploy**, set:
   - **Pre-deploy step** (**+ Add pre-deploy step**): `alembic upgrade head`
   - **Healthcheck Path**: `/health` (timeout 120 seconds, if shown)
   - **Restart Policy**: *On Failure*, max retries 10
   - **Serverless** / app sleeping: **off**

   Leave **Custom Start Command** empty (the Dockerfile's `CMD` starts the app) and **Teardown** off (step 4's draining variable handles shutdown). Also turn on **Wait for CI** (under Source), so a push that fails CI is never deployed.
6. **Deploy.** Save the variables (Railway redeploys), or press **Deploy**. In **Deployments → View logs**, the pre-deploy step shows `Running upgrade … -> 0006`, then the app logs `app_started`. Open `https://<name>.up.railway.app/health` and check that it returns `{"status":"ok"}`.
7. **Point the bot at it.** In PowerShell, from the repository (the variables override `.env` for this command only):

   ```powershell
   $env:TELEGRAM_BOT_TOKEN = "<production bot token>"
   $env:TELEGRAM_WEBHOOK_SECRET = "<the secret from step 4>"
   $env:APP_BASE_URL = "https://<name>.up.railway.app"
   uv run python scripts/telegram_webhook.py set
   Remove-Item Env:TELEGRAM_BOT_TOKEN, Env:TELEGRAM_WEBHOOK_SECRET, Env:APP_BASE_URL
   ```

   `set` checks `/health` first, then prints what Telegram has registered. `webhook url` should be your URL, with no `last error`. Run `... telegram_webhook.py info` at any time; a `401 Unauthorized` as the last error means the secret here and in Railway differ.
8. **A separate bot for local development.** Create a second bot with @BotFather (`/newbot`, then `/setjoingroups` → Disable), press **Start** in its chat, and put its token in your local `.env`. From now on `scripts/telegram_poll.py` uses that bot, so it never touches the deployed one. Your local database keeps its cases; production starts empty.
9. **Spending limit** (after moving to Hobby): **Workspace → Usage → Usage limits**. Set a hard limit such as $10, which is well above this bot's expected $5–10 a month.
10. **Backups.** If the `Postgres` service has a **Backups** tab on your plan, schedule a daily backup. Otherwise take one by hand now and then, using the `DATABASE_PUBLIC_URL` from the Postgres service's Variables (the dump contains your case data, so keep it private):

    ```powershell
    docker run --rm -v "${PWD}:/backup" postgres:18 pg_dump "<DATABASE_PUBLIC_URL>" -f /backup/swordbot-backup.sql
    ```

    The image's major version must be at least the server's (Railway runs Postgres 18; `pg_dump` refuses a newer server). `-f` writes the file inside the container: Windows PowerShell's `>` would re-encode the dump as UTF-16, which `psql` can't restore.

### Later deploys

Pushing to `main` deploys automatically, after CI passes if **Wait for CI** is on. The pre-deploy step migrates the database, and the health check gates the switch-over. To roll back, redeploy an earlier deployment from **Deployments**. Migrations are **not** rolled back, which is another reason they must stay backward compatible.

To rotate the webhook secret, change `TELEGRAM_WEBHOOK_SECRET` in Railway, wait for the redeploy, then run step 7 again with the new value. Messages that arrive in between are rejected and retried by Telegram, so none are lost.

### Verifying receipt lookup (M8)

After deploying M8 and replacing `GMAIL_REFRESH_TOKEN` with one that has both scopes:

1. `uv run python scripts/gmail_auth.py --check` reports both scopes.
2. Tell the bot about a real recent order without its number ("My DoorDash order last night was missing the fries"). It answers with the order it found and **[Yes] [No]**, not with a question about the order.
3. Type "yes". Nothing is recorded; it asks you to use the buttons. Press **Yes**: it moves on to the next missing detail, and the draft later names the order number.
4. Start another case for a merchant with no email in your inbox: it says it couldn't find the order and asks.
5. Logs: `order_emails_searched` and `receipt_read` lines with counts only, and no subjects, senders or email text anywhere.

### Verifying (M7.5)

With the laptop **off**, from your phone:

1. Open `https://<name>.up.railway.app/health` in the phone's browser: `{"status":"ok"}`.
2. Send the bot a complaint ("My DoorDash order tonight was missing the fries"), answer its questions, and give your own second address as the support email. A draft appears with **[Send] [Edit] [Cancel]**.
3. Reply "looks good" in text. Nothing is sent; only the button approves.
4. Press **Send**. The bot confirms what went out and to whom. The email is in your Gmail **Sent** folder, and the second address received it (check spam).
5. Press **Send** on the same draft again: "no longer valid", and still only one email.
6. Ask "any news on that email?". It gets a status reply about the sent case and doesn't open a new one.
7. In Railway, **Redeploy** the app, wait for it to go live, and ask again. The same answer shows the case survived the restart.
8. Railway logs: `event_done` for the `send_email` event, no `event_dead` or `startup_config_invalid`, and no email text, tokens or keys anywhere in the logs.
