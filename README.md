---
title: PocketAuditor
emoji: 💰
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---

# PocketAuditor — a personal finance agent on Telegram

Forward your bank/UPI SMS (or a photo of a receipt) to a Telegram bot. A
**tool-using LLM agent** decides what each transaction is — the same as an
expense you already logged, a new expense in a clear category, or something
to ask you about — while a **policy layer in plain code** checks every write
before it happens. It also tracks your **salary → savings → spending** month
(you save a different amount every month; nothing is assumed), warns you
before you run out, and answers questions in plain language ("how much did I
spend on food this week?"), asking you to confirm with a button before it
changes anything.

Design doc: [`docs/AGENT_DESIGN.md`](docs/AGENT_DESIGN.md).

```mermaid
flowchart LR
    SMS["Forwarded SMS / photo"] --> P["Parser<br/>regex first, LLM fallback"]
    CHAT["Chat message / /ask"] --> A
    P -->|"debit"| A["Agent loop<br/>Gemini 3.5 Flash-Lite<br/>tool calling"]
    P -->|"to/from own savings"| S["Savings flow<br/>(plain code)"]
    P -->|"credit"| I["Income (labelled)"]
    A -->|read tools| D[("Postgres")]
    A -->|write tools| POL{"Policy<br/>allow / ask / deny"}
    POL -->|allow| D
    POL -->|ask| B["Telegram buttons<br/>user decides"]
    B --> D
    A --> T["Traces<br/>agent_runs / agent_steps"]
```

### What makes it more than an LLM wrapper

| Principle | Where |
|---|---|
| **The model proposes, code disposes.** Every write tool call goes through `policy.check()`: no made-up ids, only known categories, never `Savings` on the model's own say-so, confidence threshold, double-charge check, one decision per transaction. Chat changes always need a Yes tap. | `app/agent_core/policy.py` |
| **The model never does arithmetic.** Every number (totals, spending money, savings %) is computed in Python and returned by a tool. | `app/month_plan.py`, `app/query.py`, `app/audit.py` |
| **Cheap and deterministic first.** Regex parsing, a remembered-merchant shortcut and own-account detection run with **no LLM call**. | `app/parser.py`, `app/agent_core/reconcile.py`, `app/savings.py` |
| **Fails safe.** A model error leaves the transaction pending; an undecided transaction is asked about, never guessed. Step, token and daily limits. | `app/agent_core/loop.py` |
| **Measured, not claimed.** An eval suite with an "unsafe writes" metric that must stay at 0. | `evals/` |
| **Observable.** Every model reply and tool call (args, result, policy verdict, tokens, latency) is stored. | `agent_runs`, `agent_steps` |

### Results so far (offline evals, no LLM)

| Suite | Before the v2 fixes | Now |
|---|---|---|
| Parser (27 real-world SMS shapes) | 63% correct, **8 unsafe** | 93% correct, **0 unsafe** |
| Reconcile without any LLM (24 scenarios) | — | 0 unsafe; everything that needs judgement is left pending, never guessed |

Run the same suites against Gemini/Claude to get the agent's accuracy, cost
and latency — see [Evals](#evals).

## Architecture at a glance

```
app/
  agent_core/   the tool-using agent: tools, chat_tools, policy, loop, reconcile, chat, actions, prompts
  llm/          LLMProvider Protocol (+ tool-calling types) + Gemini/Claude providers + factory
  parser.py     regex-first SMS parser, LLM fallback; also routes SMS vs. chat
  savings.py    own-account detection: transfer to savings / withdrawal from savings / income kinds
  month_plan.py spending money, money left, saved %, run-out date, money alerts
  agent.py      the original fixed pipeline (RECONCILE_ENGINE=pipeline) + shared act_on_decision
  audit.py      monthly salary audit (numbers in code, LLM writes the prose)
  income.py, salary.py, budgets.py, query.py, reports.py, digest.py, merchant_memory.py
  privacy.py    masks phone/account/card numbers before text reaches an LLM
  timeutil.py   local_today() in TIMEZONE (default Asia/Kolkata)
  telegram/     handlers (SMS, chat, photos, commands, inline buttons), allowlist
  main.py, routes.py, cron.py   FastAPI app, webhook + cron endpoints
alembic/        migrations
evals/          scenarios + runner + reports
tests/          pytest suite (all offline — no live Gemini/Telegram/DB needed)
docs/           AGENT_DESIGN.md
```

---

## Setup

### 1. Python environment

Requires **Python 3.11+** (FastAPI/Alembic/python-telegram-bot all need ≥3.10).

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt   # includes requirements.txt + test deps
```

### 2. Gemini API key (used for local development and production)

Get a key at [aistudio.google.com](https://aistudio.google.com) → "Get API
key". Put it in `.env` as `GEMINI_API_KEY`. Use a separate key for
development so it has its own quota. The tests never call Gemini — `pytest`
needs no key.

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

`LLM_PROVIDER=gemini` / `LLM_MODEL=gemini-3.5-flash-lite` are the defaults —
set `GEMINI_API_KEY`. Also set:

- `ALLOWED_CHAT_IDS` — who may use the bot (comma-separated chat ids, or `*`).
  Empty means nobody: message the bot once and it replies with your chat id.
- `TIMEZONE` — defaults to `Asia/Kolkata`; every "today"/month boundary uses it.
- `RECONCILE_ENGINE` — `agent` (default) or `pipeline` (the original flow).

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

### Monthly cron for the salary audit (GitHub Actions)

Runs on the 1st of each month and audits the **previous completed** month, so
the whole month's income and spending is in before it's judged.

```yaml
# .github/workflows/run-audit.yml
name: Monthly salary audit
on:
  schedule:
    - cron: "0 3 1 * *"   # 1st of the month, 03:00 UTC
  workflow_dispatch: {}
jobs:
  run-audit:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger monthly audit
        run: |
          curl -X POST "${{ secrets.APP_URL }}/run-audit" \
            -H "X-Cron-Secret: ${{ secrets.CRON_SECRET }}" -f
```

Same `202`-and-background-task shape as `/reconcile`. `run_monthly_audit` is
idempotent per month (the `(user_id, period_month)` row in `audit_runs` is
both the trail and the once-per-month guard), so a re-run — or the on-demand
`/audit` command — never double-audits.

### Daily cron for mid-month salary alerts (GitHub Actions)

Proactive within-the-month warnings (salary late, spending pace too high) are
checked **daily**, same reasoning as budget alerts — a "salary hasn't arrived"
or "you're on track to overspend" heads-up is only useful while there's still
month left to act on it.

```yaml
# .github/workflows/check-salary-alerts.yml
name: Daily salary alerts
on:
  schedule:
    - cron: "0 3 * * *"   # every day, 03:00 UTC
  workflow_dispatch: {}
jobs:
  check-salary-alerts:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger salary-alert check
        run: |
          curl -X POST "${{ secrets.APP_URL }}/check-salary-alerts" \
            -H "X-Cron-Secret: ${{ secrets.CRON_SECRET }}" -f
```

`check_midmonth_alerts` dedups via `audit_alerts_sent` (at most one of each
alert type per calendar month), so daily checking doesn't mean daily nagging.
Both endpoints are gated by the same `CRON_SECRET` as `/reconcile` and
`/check-budgets`.

### Weekly cron for the spend digest (GitHub Actions)

```yaml
# .github/workflows/weekly-digest.yml
name: Weekly digest
on:
  schedule:
    - cron: "0 3 * * 1"   # Monday 03:00 UTC
  workflow_dispatch: {}
jobs:
  weekly-digest:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger weekly digest
        run: |
          curl -X POST "${{ secrets.APP_URL }}/send-weekly-digest" \
            -H "X-Cron-Secret: ${{ secrets.CRON_SECRET }}" -f
```

Same `202`-and-background-task shape, gated by the same `CRON_SECRET`. Unlike
the budget/salary alerts, there's no dedup table here — a weekly cron running
once a week has no "checked more often than the natural period" problem to
solve, so a duplicate send is just a harmless repeated read-only message, the
same reasoning that already applies to `/reconcile`.

### Keep-alive ping (Render + Neon free tier)

`GET /health` returns `200` without touching the database, so a keep-alive
ping (e.g. from [cron-job.org](https://cron-job.org) or the same GitHub
Actions workflow on a shorter schedule) can hit it without waiting on a
possibly-suspended Neon compute to wake up.

---

## Choosing an LLM provider

| | `LLM_PROVIDER=gemini` (default) | `LLM_PROVIDER=claude` |
|---|---|---|
| Use | Local development **and** production | Only to compare in the evals |
| Model | `gemini-3.5-flash-lite` | e.g. `claude-sonnet-5` |
| Key | `GEMINI_API_KEY` | `ANTHROPIC_API_KEY` |

Both implement every `LLMProvider` method, including `chat_with_tools` for
the agent. Any SDK or network error becomes `LLMDecisionError`, which the
app already handles (the transaction stays pending, or the chat says
"try again"). Note: on Gemini's free tier Google may use prompts to improve
its models — account/phone numbers are masked first (`app/privacy.py`).

---

## The agent (v2)

Every forwarded debit SMS is decided right away (no need to run
`/reconcile`, though it still works for anything left pending):

1. **Remembered merchant** (exact or fuzzy — "ZOMATO LTD" ≈ "zomato@hdfc")
   and nothing to match → logged by plain code, no LLM call.
2. Otherwise the agent gets the transaction and these tools:
   `find_matching_expenses`, `recall_merchant`, `check_duplicate_charge`
   (read) and `link_to_expense`, `log_as_new_expense`,
   `ask_user_for_category` (write, checked by the policy).
3. If the agent ends without deciding, you're asked. If the model is down,
   the transaction stays pending and is retried.

Any text that isn't a bank SMS goes to the **chat agent** (so does `/ask`).
It reads with `get_month_plan`, `get_spending`, `get_balance`,
`list_expenses`, `get_budgets`, `get_salary_settings`, and proposes changes
with `set_budget`, `recategorize_expense`, `log_cash_expense`,
`remember_savings_account` — each one is shown to you as a Yes/No question
and runs only after you tap Yes. It remembers the last 10 messages, so
follow-ups like "and last week?" work.

Limits (in `.env`): `AGENT_MAX_STEPS=8`, `AGENT_MAX_TOKENS_PER_RUN=40000`,
`AGENT_MAX_RUNS_PER_DAY=200`. Inspect a run with SQL:

```sql
select step_no, kind, tool_name, verdict, args, result, latency_ms
from agent_steps where run_id = '<id>' order by step_no;
```

## Salary → savings → spending

You save a different amount every month, so nothing assumes a percentage:

1. `/myaccount savings 4321` once (last 4 digits of your savings account).
2. Forward your salary SMS → recorded as income of kind `salary`.
3. Forward the SMS when you move money to savings (any amount, any number of
   times) → filed as `Savings`, not spending.
4. Spend as usual. If you take money back from savings, forward that SMS →
   recorded as `savings_withdrawal`, **not** income.

`/plan` shows the month so far:

```
spending money = salary + other income − moved to savings + taken back
money left     = spending money − spent
```

Alerts (each at most once a month): half used, only 20% left, ran out, and
"at this pace you run out around the 22nd".

---

## Photo receipts (Phase 2)

Send a photo of a paper bill, UPI payment confirmation screenshot, or
grocery/fuel receipt and the bot extracts merchant, total, date, and line
items, then creates a `transactions` row (`source='photo'`) that goes through
the exact same reconciliation loop as an SMS-sourced one.

**Vision provider requirement:** this uses the LLM's native vision, not a
separate OCR library. Gemini 3.5 Flash-Lite (the default) and Claude both
accept images.

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
parser's `is_transaction`/`is_financial_question` guards below for why. `/log`
is the explicit, predictable way to do this instead — same reasoning as
`/ask` being a command rather than passive listening.

---

## Merchant memory & duplicate detection

Two small, deterministic additions to the reconcile loop, on top of the
guards above.

**Merchant memory.** Once you answer an `ask_user` question for a merchant
(e.g. picking "Food" for "Swiggy"), that choice is remembered
(`app/merchant_memory.py`, the `merchant_categories` table). The next
transaction from the same merchant — with no manual expense candidate to
consider instead, and no more specific category hint already at hand for
that transaction — skips the LLM call entirely and auto-logs straight from
memory. A photo caption or a manual candidate always takes priority over a
remembered default; memory only fills in when there's nothing more specific
to go on.

**Duplicate-charge guard.** A new transaction with the same merchant and
amount as an expense already logged within the last couple of days
(`duplicate_charge_window_days`, default 2) gets asked about instead of
silently auto-linked/auto-logged — a same-merchant, same-amount repeat is
often a genuine double-debit, not two separate purchases. This is a
deliberate friction cost for legitimate same-day repeat purchases (two
identical coffees, say) in exchange for catching real double-debits before
they quietly become two expenses instead of one.

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

## Chat questions (replaces the old `/ask` query builder)

Just type a question — or use `/ask <question>`. The chat agent answers from
tools that run parameterized queries (never SQL from the model) and repeats
their numbers. See [The agent (v2)](#the-agent-v2).

## Salary audit (Phase 4)

Phase 1–3 only ever tracked money *out*. Phase 4 adds the money-*in* side and a
monthly auditor on top of it.

**Income capture.** A forwarded **credit** SMS used to be discarded; it's now
parsed (the parser already reads direction and the counterparty/employer name)
and written to a separate `incomes` ledger via `app/income.py:record_income`.
Income is deliberately **not** an expense: it never becomes a `transactions`
row, never enters `reconcile_user`, and never counts toward `/spend` or budgets
(both of which sum `expenses`). `/income` shows income totals for this month,
last month, this year, and all time.

**Salary profile.** `/salary <expected> [savings_target] [payday]` (e.g.
`/salary 50000 10000 1`) records what you expect to earn, an optional monthly
savings target, and an optional payday (day-of-month). This is what grounds the
"was salary received / on time" finding and the mid-month alerts — without an
expected figure there's nothing to compare actual income against. `/salary`
with no arguments shows the current profile.

**The monthly audit — an AI agent, structured like `reconcile_user`.**
`/audit` (and the 1st-of-month cron) runs `app/audit.py:run_monthly_audit` over
the previous completed month: perceive (load the month's income, spend,
previous month for deltas, budgets, salary profile) → compute a deterministic
`FinancialSnapshot` (totals, savings rate, per-category month-over-month deltas,
whether salary landed, and a shortlist of anomaly candidates) → decide
(`provider.audit_finances(snapshot)`) → act (send the report, ask about
anomalies).

The load-bearing rule from `/ask` and `_apply_guard` holds here too: **every
number is computed in code; the LLM only writes prose.** The model receives the
pre-computed snapshot and returns *only* a summary, recommendations, and which
of the offered anomaly candidates to flag — never a figure. `_apply_audit_guard`
then drops any flagged expense id that wasn't in the shortlist we offered (the
same hallucination check `_apply_guard` applies to a match's expense id). If the
LLM call fails, the numbers still go out on their own — same resilience as the
parser falling back to its regex result.

Flagged anomalies (e.g. an expense left `Uncategorized`, or an unusually large
one) become inline-button questions via a distinct `acat:` callback that
recategorizes the *existing* expense in place — separate from reconcile's `cat:`
flow, which *creates* an expense from a pending transaction.

**Proactive mid-month alerts.** `check_midmonth_alerts` (daily cron,
deterministic — no LLM) fires at most once each per month: `salary_late` (payday
+ grace has passed and no credit matching the expected salary has arrived) and
`pace_high` (month-to-date spend, projected to month-end, exceeds the
income-minus-savings-target ceiling). Dedup is via `audit_alerts_sent`, the same
insert-before-send guard budgets use.

See [Monthly cron for the salary audit](#monthly-cron-for-the-salary-audit-github-actions)
and [Daily cron for mid-month salary alerts](#daily-cron-for-mid-month-salary-alerts-github-actions)
for the schedules.

---

## Weekly digest

A lightweight, proactive, **fully deterministic** (no LLM call) weekly
message (`app/digest.py`, the weekly `/send-weekly-digest` cron): this
week's total spend, the month so far, the top spending category this week,
and the biggest single expense this week. Excludes the `Savings` category
the same way `/spend`, the monthly audit, its `pace_high` alert, and `/ask`'s
`net` aggregation already do — this is the fifth call site applying that
exclusion. No dedup table (see
[Weekly cron for the spend digest](#weekly-cron-for-the-spend-digest-github-actions)
above for why); no on-demand command — this is cron-only, same as the budget
and salary alerts.

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

All tests are offline (mocked HTTP/API calls, aiosqlite for DB tests) — no
live Gemini/Claude, Telegram or Neon connection required. Covers:

- **Agent core** (`tests/test_agent_core.py`) — a scripted model drives the
  real loop: logging, linking, every policy rule (hallucinated id, low
  confidence, invented category, Savings, double charge, caption-less photo,
  double decision), unknown tools/bad arguments, model failure, step and
  daily limits, the no-LLM memory shortcut, and both providers'
  tool-calling conversions.
- **Chat + savings** (`tests/test_chat_and_savings.py`) — chat answers from
  tools, Yes/No approvals (including another user trying to approve), the
  full varying-savings month from the design doc, money alerts.
- **Phase 0 regressions** (`tests/test_phase0_regressions.py`) — one test per
  fixed bug.
- **Evals** (`tests/test_evals.py`) — the offline eval suites must write 0
  unsafe data.
- **Provider parity** (`tests/test_llm_providers.py`,
  `tests/test_agent_provider_parity.py`, `tests/test_audit_provider_parity.py`)
  — identical mocked model output through `GeminiProvider` and
  `ClaudeProvider` gives equal results, for every structured-output method;
  SDK/network errors surface as `LLMDecisionError`.
- **Parser** (`tests/test_parser.py`) — realistic HDFC/ICICI/SBI/UPI SMS
  fixtures resolved by regex alone; low-confidence/garbage messages routed to
  the LLM fallback.
- **Agent loop** (`tests/test_agent.py`) — all three actions, both
  code-level conservatism guards (low confidence, hallucinated match id),
  per-transaction error isolation, candidate exclusion, the merchant-memory
  short-circuit (including that it's skipped when a candidate or a
  category_hint exists, and that it still auto-logs a caption-less photo
  transaction), and the duplicate-charge guard (window boundary, merchant
  mismatch, both auto_link and auto_log downgraded).
- **Merchant memory** (`tests/test_merchant_memory.py`,
  `tests/test_expenses.py`) — normalization round-trips, per-user scoping,
  overwrite-not-duplicate on a second `ask_user` answer for the same
  merchant, no-op on a merchant-less transaction.
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
- **Income & salary** (`tests/test_income.py`, `tests/test_salary.py`,
  `tests/test_ingestion.py`) — a credit SMS now records an `Income` row (never
  a transaction), income summary bucketing, salary-profile upsert/validation.
- **Salary audit** (`tests/test_audit.py`) — snapshot totals/savings-rate,
  salary-received detection, category deltas, anomaly-candidate selection, the
  hallucination guard, monthly idempotency, numbers-still-report-when-LLM-fails,
  and the salary/savings-target report lines.
- **Mid-month alerts** (`tests/test_midmonth_alerts.py`) — `salary_late` and
  `pace_high` firing conditions, the grace period, the early-month pace
  suppression, and once-per-month dedup.
- **Audit commands/callbacks** (`tests/test_telegram_audit_handler.py`,
  `tests/test_audit_callback.py`, `tests/test_telegram_income_handler.py`,
  `tests/test_telegram_salary_handler.py`) — `/audit` report + anomaly
  questions, the `acat:` recategorize round trip, `/income`, `/salary`.
- **Weekly digest** (`tests/test_digest.py`, `tests/test_reports.py`) —
  reuse of `get_spend_summary`'s totals via an injected `today`, top-category
  and biggest-expense queries, the Savings exclusion, zero-activity, and a
  deterministic tie-break on the biggest-expense query.
- **FastAPI wiring** (`tests/test_main.py`) — webhook secret check, and
  `/reconcile`, `/check-budgets`, `/run-audit`, `/check-salary-alerts`,
  `/send-weekly-digest` background-task scheduling (each fail-closed on
  `CRON_SECRET`), `/health`.

To sanity-check the provider abstraction against a **real** model (not
mocks — this is what actually proves the abstraction holds, since the unit
tests only prove it against mocked responses):

```bash
python -m scripts.try_decide           # uses LLM_PROVIDER from .env
python -m scripts.try_decide --both    # Gemini and Claude side by side (needs ANTHROPIC_API_KEY)
```

(This script now also exercises `interpret_query` alongside `decide_match`
and `parse_transaction`. `--both` compares Gemini and Claude specifically;
to try Gemini instead, just set `LLM_PROVIDER=gemini` in `.env` and run
without `--both` — `get_provider()` picks it up like any other provider.)

---

## Evals

```bash
python -m evals.run --suite parser --provider none          # offline
python -m evals.run --suite reconcile --provider none --engine agent
python -m evals.run --suite reconcile --provider gemini --engine agent
python -m evals.run --suite reconcile --provider gemini --engine pipeline
python -m evals.run --suite reconcile --provider claude --model claude-sonnet-5 --engine agent
```

Reports land in `evals/results/`. "Unsafe" (wrong money data written) must
be 0; the offline suites run in CI (`tests/test_evals.py`). See
[`evals/README.md`](evals/README.md).

## What's not built yet

- Multi-currency; voice notes; line items of a receipt as separate expenses.
- A credit SMS *from the savings account's own bank* ("A/c XX4321 credited
  with ...") is treated as ordinary income — forward the SMS from your salary
  account for transfers.
- Agent runs are not resumed after a Yes/No tap: the approved change runs in
  plain code and the conversation continues from there.
- The monthly audit and alerts stay deterministic with LLM-written prose
  only; they are not run through the tool-using agent.
