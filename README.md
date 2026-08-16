---
title: PocketAuditor
emoji: 💰
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---

# PocketAuditor — Statement Reconciliation Agent

A Telegram bot that reconciles bank/UPI transactions against a manual expense
ledger. A transaction can come from a forwarded SMS/UPI text (Phase 1) or a
photo of a bill/receipt/payment confirmation (Phase 2) — both feed the same
`transactions` table and the same decision loop. For each unprocessed
transaction, an LLM-backed agent decides whether to:

- **`auto_link`** — match it to an expense you already logged
- **`auto_log`** — log it as a new expense on its own
- **`ask_user`** — ask you to pick a category, via a single Telegram message with inline buttons

Every decision (and its reasoning) is written to `reconciliation_runs` for
auditability. The LLM sits behind one interface (`LLMProvider`) so a local
Ollama model (dev), Claude, and Gemini are all drop-in interchangeable — see
`app/llm/` and [Choosing an LLM provider](#choosing-an-llm-provider) below.

Phase 3a adds monthly per-category budgets (`/setbudget`, `/budgets`) with a
deterministic (no-LLM) alert when a category crosses 80% of its limit — see
[Budget alerts](#budget-alerts-phase-3a) below. Phase 3b adds `/ask`, a
natural-language query over your expense ledger, via a constrained query
intent the LLM fills in — never raw SQL — see
[NL query chat](#nl-query-chat-phase-3b) below.

## Architecture at a glance

```
app/
  config.py, db.py, models.py, schemas.py   settings, DB, ORM models, Pydantic schemas
  llm/          LLMProvider Protocol + Ollama/Claude/Gemini providers + factory
  parser.py     regex-first SMS parser, LLM fallback for ambiguous messages
  agent.py      perceive -> compare -> decide -> act loop
  budgets.py    deterministic budget-threshold alerts — no LLM involved
  query.py      QueryIntent -> parameterized SQLAlchemy query -> phrased answer
  telegram/     bot handlers (SMS text, photo receipts, /reconcile, /log, /setbudget, /budgets, /ask, category callback)
  main.py       FastAPI app: /telegram/webhook, /reconcile, /check-budgets, /health
alembic/        migrations
tests/          pytest suite (all offline — no live Ollama/Telegram/DB needed)
```

`app/agent.py` and `app/parser.py` only ever import `LLMProvider` and the
Pydantic schemas — never `anthropic` or `httpx` directly — which is what
keeps the provider swap (and the test suite) cheap.

---

## Setup

### 1. Python environment

Requires **Python 3.11+** (FastAPI/Alembic/python-telegram-bot all need ≥3.10).

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt   # includes requirements.txt + test deps
```

### 2. Ollama (local dev model)

```bash
brew install ollama          # or see https://ollama.com/download
ollama serve                 # if not already running as a background service
ollama pull qwen2.5vl:7b
```

Verify it's up: `curl http://localhost:11434/api/tags`

`qwen2.5vl:7b` (Qwen2.5-VL, the default `LLM_MODEL`) handles all four
`LLMProvider` methods — `decide_match`, `parse_transaction`, `extract_receipt`,
*and* `interpret_query` — since it's vision-capable as well as text-capable,
same family as the original text-only `qwen2.5:7b`. See
[Photo receipts](#photo-receipts-phase-2) below for more on the vision side.

### 3. Neon Postgres

Create a free project at [neon.tech](https://neon.tech), then grab the
connection string from the dashboard. **Two gotchas** when adapting it for
this project:

- Neon's default string is `postgresql://...`, but the app uses the
  **asyncpg** driver — add the `+asyncpg` suffix: `postgresql+asyncpg://...`.
- Neon's string usually includes `?sslmode=require&channel_binding=require`.
  asyncpg doesn't understand `channel_binding` at all (it's a libpq-only
  parameter) and will refuse to connect if it's present — replace the query
  string with `?ssl=require`.

  ```
  # Neon gives you:
  postgresql://user:pass@ep-xxx-pooler.region.aws.neon.tech/db?sslmode=require&channel_binding=require
  # You want:
  postgresql+asyncpg://user:pass@ep-xxx-pooler.region.aws.neon.tech/db?ssl=require
  ```

If you're on Neon's **pooled** (pgbouncer, transaction-mode) endpoint —
which is the default and what the `-pooler` hostname indicates — no further
change is needed; `app/db.py` already disables asyncpg's server-side
prepared-statement cache (`statement_cache_size=0`), which is what avoids
`DuplicatePreparedStatementError` under a pooled connection.

### 4. Telegram bot token

Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`,
follow the prompts. You'll get a token like `123456789:AAH...`.

### 5. Configure `.env`

```bash
cp .env.example .env
# fill in DATABASE_URL and TELEGRAM_BOT_TOKEN at minimum
```

`LLM_PROVIDER=ollama` / `LLM_MODEL=qwen2.5vl:7b` are the defaults and need
nothing else. Only set `ANTHROPIC_API_KEY` if you switch to
`LLM_PROVIDER=claude`, or `GEMINI_API_KEY` if you switch to
`LLM_PROVIDER=gemini` — see
[Choosing an LLM provider](#choosing-an-llm-provider) below for the tradeoffs.

### 6. Run the migration

```bash
alembic upgrade head
```

Creates `users`, `transactions`, `expenses`, `reconciliation_runs`, `budgets`,
`budget_alerts_sent`.

---

## Running it

### Local dev (polling — no public URL needed)

```bash
set -a && source .env && set +a
export RUN_MODE=polling
uvicorn app.main:app --reload --host 127.0.0.1 --port "${PORT:-7001}"
```

The bot long-polls Telegram directly; no webhook registration needed. This
is the easiest way to iterate locally. `--port "${PORT:-7001}"` picks up
whatever `PORT` you've set in `.env` (falling back to 7001 if unset) —
uvicorn doesn't read `.env` on its own, so this is what keeps the two in sync.
Note: port 7000 itself is taken on macOS by AirPlay Receiver (ControlCenter),
which is why 7001 is the default here instead.

### Production-style (webhook)

Render's free tier sleeps on idle and wakes on incoming HTTP — a long-poller
would fight the web service for the connection and get killed on sleep, so
production runs in webhook mode instead (`RUN_MODE=webhook`, the default).
Once deployed, point Telegram at your public URL:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -d "url=https://<your-app>.onrender.com/telegram/webhook" \
  -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
```

Set the same `TELEGRAM_WEBHOOK_SECRET` value in `.env` — the webhook route
checks it against the `X-Telegram-Bot-Api-Secret-Token` header Telegram sends
back, rejecting anything else with 401. This check fails *closed*: if
`TELEGRAM_WEBHOOK_SECRET` isn't set, the route rejects every request,
including real Telegram updates, rather than accepting unauthenticated ones.

For local webhook testing without deploying, tunnel with `ngrok http "$PORT"`
(matching whatever `PORT` you set in `.env`) and `setWebhook` against the
ngrok URL instead.

### Weekly cron (GitHub Actions)

`/reconcile` and `/check-budgets` both require an `X-Cron-Secret` header
matching `CRON_SECRET` in `.env` — like the webhook secret, this fails
*closed*: leaving `CRON_SECRET` unset rejects every caller with 401 rather
than letting anyone who finds `APP_URL` trigger an LLM run for every user in
the database. Add `CRON_SECRET` as a repo secret alongside `APP_URL`.

```yaml
# .github/workflows/reconcile.yml
name: Weekly reconcile
on:
  schedule:
    - cron: "0 3 * * 1"   # Monday 03:00 UTC
  workflow_dispatch: {}
jobs:
  reconcile:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger reconciliation
        run: |
          curl -X POST "${{ secrets.APP_URL }}/reconcile" \
            -H "X-Cron-Secret: ${{ secrets.CRON_SECRET }}" -f
```

`POST /reconcile` returns `202` immediately (it schedules the actual loop as
a background task) — this endpoint has no Telegram chat context, so unlike
the `/reconcile` **command**, it loops over every known user in the DB, not
just one chat. Each user's summary is pushed to their own chat when their
loop finishes.

### Daily cron for budget alerts (GitHub Actions)

Budget alerts are deliberately checked **daily**, not weekly like
reconciliation — an alert at "80% used, 4 days left" is only useful if it
arrives with enough of the month left to act on it. A weekly check could mean
you're already over budget for up to 6 days before hearing about it.

```yaml
# .github/workflows/check-budgets.yml
name: Daily budget check
on:
  schedule:
    - cron: "0 3 * * *"   # every day, 03:00 UTC
  workflow_dispatch: {}
jobs:
  check-budgets:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger budget check
        run: |
          curl -X POST "${{ secrets.APP_URL }}/check-budgets" \
            -H "X-Cron-Secret: ${{ secrets.CRON_SECRET }}" -f
```

Same `202`-and-background-task shape as `/reconcile`: it loops over every
user, and `check_budget_alerts` itself guarantees at most one alert per
category per calendar month (via `budget_alerts_sent`), so running this daily
doesn't mean daily *nagging* — just a daily *check*.

### Keep-alive ping (Render + Neon free tier)

`GET /health` returns `200` without touching the database, so a keep-alive
ping (e.g. from [cron-job.org](https://cron-job.org) or the same GitHub
Actions workflow on a shorter schedule) can hit it without waiting on a
possibly-suspended Neon compute to wake up.

---

## Choosing an LLM provider

Three interchangeable `LLMProvider` implementations exist (`app/llm/ollama.py`,
`app/llm/claude.py`, `app/llm/gemini.py`) — all four methods
(`decide_match`, `parse_transaction`, `extract_receipt`, `interpret_query`),
same structured-output contract, swap with one `.env` line.

| | `LLM_PROVIDER=ollama` | `LLM_PROVIDER=claude` | `LLM_PROVIDER=gemini` |
|---|---|---|---|
| Cost | Free (your hardware) | Pay-per-request | **Free** (generous daily quota) |
| Setup | `ollama serve` running locally | `ANTHROPIC_API_KEY` | `GEMINI_API_KEY` |
| Where it runs | Your machine only | Cloud — deployable anywhere | Cloud — deployable anywhere |
| Data privacy | Never leaves your machine | Anthropic's standard API terms | **Free tier: Google may use prompts/responses to improve their models** |

**Gemini is the recommended free option if you want this deployed somewhere
other than your own laptop** (see [Running it](#running-it) — a long-poller
needs *something* up 24/7). Get a key at
[aistudio.google.com](https://aistudio.google.com) → "Get API key" → "Create
API key" — no credit card, no billing account, genuinely free with no
expiration. Set `LLM_MODEL` to one of Google's `-latest` aliases
(`gemini-flash-lite-latest` is the default here) rather than a pinned version
like `gemini-2.5-flash` — Google retires specific dated model versions for
new API keys fairly often (that exact 404 happened during this project's own
setup), while the `-latest` aliases keep pointing at whatever's current.

One caveat worth knowing: Gemini's free tier occasionally returns a `503
UNAVAILABLE` ("high demand") error on `-latest`-alias models, especially the
non-lite tier — this surfaces as `LLMDecisionError` same as any other
provider hiccup (see `app/llm/base.py`'s `LLMDecisionError` contract), so
existing retry/guard/graceful-fallback behavior already covers it; it just
means an occasional message needs a retry rather than being unusable.

---

## Photo receipts (Phase 2)

Send a photo of a paper bill, UPI payment confirmation screenshot, or
grocery/fuel receipt and the bot extracts merchant, total, date, and line
items, then creates a `transactions` row (`source='photo'`) that goes through
the exact same reconciliation loop as an SMS-sourced one.

**Vision provider requirement:** this uses the LLM's native vision, not a
separate OCR library. The default `LLM_MODEL` (`qwen2.5vl:7b`) is
vision-capable, so `LLM_PROVIDER=ollama` handles this locally — one model
covers `decide_match`, `parse_transaction`, and `extract_receipt`. Claude
(`LLM_PROVIDER=claude`) remains a fully-supported alternative (e.g. for prod,
or if a heavier/better vision model is worth the API cost). Note:
`OllamaProvider.extract_receipt` doesn't independently verify the configured
model is vision-capable — if you revert `LLM_MODEL` to a text-only model
(e.g. `qwen2.5:7b`), extract_receipt's behavior depends on how that model and
Ollama handle an unsupported `images` field, which hasn't been characterized
here; the working, tested configuration is `qwen2.5vl:7b` or Claude.

**Low-quality images degrade gracefully, not silently.** The model sets
`readable=false` on its own output whenever it can't trust what it read
(blurry, dark, cropped, or not a receipt at all); the bot also checks
`confidence` against `CONFIDENCE_THRESHOLD` and rejects a missing
`total_amount` outright. Any of those cause a reply asking you to retake the
photo or type the expense manually — no transaction is created.

**Telegram's own image handling** matters for what actually reaches the
model:

- Photos sent normally (not "as file") are resized by Telegram's servers to
  fit within roughly 1280×1280 before the bot ever sees them — the bot
  requests the largest of the sizes Telegram offers, but that's already a
  compressed JPEG, not your camera's original. A long, small-print grocery
  receipt can lose legibility this way even before the LLM sees it.
- Bots can download files up to 20 MB via `getFile` — a compressed photo is
  well under that in practice, so this is rarely the limiting factor.
- Sending the original as a *file* (uncompressed) instead of a *photo* would
  preserve resolution, but the bot only registers a `filters.PHOTO` handler
  right now, not `filters.Document.IMAGE` — that's a possible follow-up if
  compression turns out to hurt real receipts, not something built in Phase 2.

**Caption = your explicit category choice.** Send the photo with a caption
matching one of the known categories (e.g. "Food") and it's stored as
`transactions.category_hint` — the agent loop trusts this over its own
guess (`app/agent.py:_apply_category_hint` overrides `suggested_category`
with it outright for `auto_log`, regardless of what the model itself
suggests). **No caption (or a caption that isn't a recognized category) on
a photo means the reconciliation loop will *ask* rather than guess** —
`auto_log` gets downgraded to `ask_user` specifically for photo-sourced
transactions with no hint, since an unfamiliar merchant name (e.g. a
personal name from a P2P transfer screenshot) is exactly the case a model
shouldn't be confidently auto-categorizing. This particular guard is unique
to photos — SMS text has no caption concept to hint from. A related but
broader guard applies to *both* sources, though: `auto_log`'s
`suggested_category` must match one of the fixed `CATEGORIES` (`app/categories.py`)
or it gets downgraded to `ask_user` too — found live when a real SMS for a
recognizable merchant ("MUTHU SUPER MARKET") got auto-logged as `"Groceries"`,
a category that could never match a `"Food"` budget the user had actually
set, silently making that spend invisible to `/budgets`.

---

## Manual entries (`/log`)

`/log <category> <amount> [notes]` (e.g. `/log Food 900 lunch with friends`)
logs an expense directly — for spend with no bank/UPI trail at all (physical
cash) that SMS/photo can never see in the first place. Unlike the
SMS/photo/`ask_user` paths, there's no `Transaction` behind this at all; it
creates the `Expense` straight away (`created_via='manual'`, no
`linked_transaction_id`). `category` is validated the same way `/setbudget`
validates it (see below) — reject early rather than let a typo silently
create an expense in a category no budget will ever match.

Forwarding plain text that isn't a real bank/UPI alert (e.g. typing `food
900` directly instead of using `/log`) is deliberately rejected rather than
guessed at — see [NL query chat](#nl-query-chat-phase-3b)'s and the SMS
parser's `is_transaction`/`is_expense_question` guards below for why. `/log`
is the explicit, predictable way to do this instead — same reasoning as
`/ask` being a command rather than passive listening.

---

## Budget alerts (Phase 3a)

`/setbudget <category> <amount>` (e.g. `/setbudget Food 4000`) sets a
monthly limit; `/budgets` shows current limits against this month's spend.
`category` must match one of the same categories the `ask_user` inline
buttons use (case-insensitively) — `app/categories.py:normalize_category`
enforces this at input time (the same canonical vocabulary `/log` and the
agent's own `auto_log` category are constrained to — see
[Photo receipts](#photo-receipts-phase-2) above). Matching a budget against actual spend is
*also* case-insensitive (`app/budgets.py:_month_spend_by_category`) —
`expenses.category` isn't a strict enum (an `auto_log` category can come
from the model's own guess or a photo's `category_hint`, not guaranteed to
match `/setbudget`'s exact casing), so both sides of the comparison are
lowercased.

**Deterministic, not LLM-backed** — `app/budgets.py` is pure SQL aggregation
against `expenses` (`SUM(amount) WHERE txn_date >= this month's start GROUP BY
category`), compared against `budgets.monthly_limit`. A category crossing 80%
of its limit fires an alert; `budget_alerts_sent` (keyed on user, category,
and first-of-month date) makes sure it fires **once** per category per
calendar month, not on every check. A category with spend but no budget row
is simply never considered — not an error, just nothing to alert on.

Checked daily via `POST /check-budgets` — see
[Daily cron for budget alerts](#daily-cron-for-budget-alerts-github-actions)
above for why daily rather than weekly.

---

## NL query chat (Phase 3b)

`/ask <question>` answers a single, self-contained question about your
expense ledger, e.g. `/ask how much did I spend on food this week` or
`/ask biggest expense last month`. Each `/ask` is independent — there's no
multi-turn refinement ("what about last month?" as a follow-up starts over,
it isn't remembered).

**Constrained query builder, not free-form SQL generation.** The LLM never
sees or produces SQL. `LLMProvider.interpret_query(question)` fills in a
small, fixed `QueryIntent` (category, one of six date-range enums, an
aggregation type, plus a one-sentence summary) — the *only* thing an LLM ever
produces for `/ask`. `app/query.py:run_query` is the one hand-written place
that turns a `QueryIntent` into a real, parameterized SQLAlchemy query
against `expenses`; every value (including `category`, matched
case-insensitively) is bound as a query parameter, never concatenated into
SQL text — so nothing in the question, however it's phrased, can alter the
query's structure. `tests/test_query.py` proves this directly: a `category`
containing `'; DROP TABLE expenses; --` is treated as an inert literal string
that simply matches nothing.

**Answers are phrased by string formatting, not a second LLM call** — e.g.
`"You spent Rs.500.00 on Food this week across 2 transactions."` The numbers
in the reply must be exactly what the query returned; a model re-stating
them is one more chance (however small) to transcribe them wrong. Same
reasoning as `agent.py`'s `_apply_guard` re-checking a decision in code
rather than trusting the model's own arithmetic.

**Guardrails:**
- A `date_range`/`category` combination that matches no expenses replies
  plainly — `"No expenses found for that period."` — rather than a confusing
  empty answer.
- If `interpret_query`'s structured output fails validation twice (same
  retry pattern as Phase 1's `decide_match`), the reply falls back to "I
  couldn't understand that question — try phrasing it like 'how much on food
  this week?'"
- `aggregation="list"` replies cap at 10 items, with a "…and N more" note if
  the actual count is higher.

---

## Testing

```bash
pytest
```

Runs automatically on every push/PR to `main` via
`.github/workflows/ci.yml` — no secrets required, since the whole suite is
offline (`DATABASE_URL`/`TELEGRAM_BOT_TOKEN` in that workflow are just
placeholders to satisfy `Settings()`'s required fields at import time; the
tests themselves never connect to Postgres or Telegram). This is CI only —
Render keeps deploying on every push to `main` exactly as it already did;
this workflow doesn't gate that.

137 tests, all offline (mocked HTTP/API calls, aiosqlite for DB tests) — no
live Ollama, Telegram, Neon, or Gemini/Claude connection required. Covers:

- **Provider parity** (`tests/test_llm_providers.py`,
  `tests/test_agent_provider_parity.py`) — identical mocked model output run
  through `OllamaProvider`, `ClaudeProvider`, and `GeminiProvider` (and
  through the full agent loop) produces byte-for-byte equal results across
  all three, for every method including `interpret_query`.
  `test_llm_providers.py` also covers `extract_receipt` (a clear receipt, a
  blurry one, a non-receipt image — all assert on `readable` rather than a
  hallucinated total) and that an Ollama-side HTTP failure (e.g. a
  non-vision model rejecting the request) surfaces as `LLMDecisionError`,
  never a raw `httpx` exception.
- **Parser** (`tests/test_parser.py`) — realistic HDFC/ICICI/SBI/UPI SMS
  fixtures resolved by regex alone; low-confidence/garbage messages routed to
  the LLM fallback.
- **Agent loop** (`tests/test_agent.py`) — all three actions, both
  code-level conservatism guards (low confidence, hallucinated match id),
  per-transaction error isolation, candidate exclusion.
- **Source parity** (`tests/test_agent_source_parity.py`) — the same
  scenario (all three actions plus the guard) run once with
  `source='sms'` and once with `source='photo'` produces identical outcomes,
  proving photo capture is additive to `reconcile_user`, not a parallel path.
- **Photo handler** (`tests/test_telegram_photo_handler.py`) — a readable
  receipt creates a `source='photo'` transaction; an unreadable one, a
  low-confidence one, and both provider failure modes
  (`NotImplementedError`, `LLMDecisionError`) all create no transaction and
  reply asking for a retake or manual entry instead.
- **Budget alerts** (`tests/test_budgets.py`) — upsert-in-place, the 80%
  threshold, no re-fire within the same month (but firing again next month),
  only current-month spend counting toward the limit, and a budget-less
  category being silently skipped.
- **Budget commands** (`tests/test_telegram_budget_handlers.py`) —
  `/setbudget` category normalization/validation, amount validation, upsert
  behavior; `/budgets` listing.
- **Query builder** (`tests/test_query.py`) — `resolve_date_range` for every
  named range plus custom bounds (including a year-boundary "last month"
  case and the missing-custom-dates fallback); each aggregation
  (`sum`/`count`/`max`/`list`) against seeded data; the `list` 10-item cap;
  case-insensitive category matching; per-user isolation; and the actual
  safety property — a `category` containing `'; DROP TABLE expenses; --` is
  treated as an inert literal that matches nothing, not SQL.
- **`/ask` handler** (`tests/test_telegram_ask_handler.py`) — correct
  numeric answers for `sum`/`max` questions against seeded data, the
  no-results guardrail, the `interpret_query`-failure fallback, usage-on-empty
  question, and that `run_query` only ever receives the validated
  `QueryIntent` object, never the raw question text.
- **FastAPI wiring** (`tests/test_main.py`) — webhook secret check,
  `/reconcile` and `/check-budgets` background-task scheduling, `/health`.

To sanity-check the provider abstraction against a **real** model (not
mocks — this is what actually proves the abstraction holds, since the unit
tests only prove it against mocked responses):

```bash
python -m scripts.try_decide           # uses LLM_PROVIDER from .env
python -m scripts.try_decide --both    # Ollama and Claude side by side (needs ANTHROPIC_API_KEY)
```

(This script now also exercises `interpret_query` alongside `decide_match`
and `parse_transaction`. `--both` compares Ollama and Claude specifically;
to try Gemini instead, just set `LLM_PROVIDER=gemini` in `.env` and run
without `--both` — `get_provider()` picks it up like any other provider.)

---

## What's not built yet

Per the Phase 1/2/3 briefs: no voice transcription, no line-item-level
categorization of a receipt's individual items (the whole receipt is one
transaction), no multi-photo/multi-page receipt stitching, no
predictive/forecasting budget features ("will I go over budget?"), no
spending recommendations or coaching tone — the bot reports facts, it
doesn't lecture. `ask_user` is a single question with inline buttons, and
`/ask` is a single self-contained question — neither is a multi-turn
dialogue; asking a follow-up starts over rather than being remembered.
