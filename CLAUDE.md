# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PocketAuditor: a Telegram bot that started (Phase 1) as a reconciler of
forwarded bank/UPI SMS against a manually-kept expense ledger. For each
unprocessed transaction, an LLM-backed agent decides `auto_link` (match an
existing expense), `auto_log` (create a new expense on its own), or `ask_user`
(ask via inline buttons). Every decision is written to `reconciliation_runs`
for audit.

Phase 4 adds a second, proactive side: a **personal salary auditor**. Credit
SMS are captured as income (`incomes` ledger, `/income`), the user registers an
expected salary (`salary_profiles`, `/salary`), and a monthly agent
(`app/audit.py`, `/audit` + a 1st-of-month cron) reports income vs. spend vs.
savings with LLM-written recommendations, asks about anomalies, and — mid-month,
deterministically — warns about a late salary or a too-high spending pace. See
"The salary audit is a second agent loop" below.

## Commands

```bash
# Setup
source venv/bin/activate
pip install -r requirements-dev.txt   # requirements.txt + test deps
alembic upgrade head                  # apply migrations (reads DATABASE_URL from .env)

# Tests — all offline (mocked HTTP/API, aiosqlite for DB tests), no live
# Ollama/Telegram/Neon connection required
pytest
pytest tests/test_agent.py                                    # one file
pytest tests/test_agent.py::test_auto_link_links_expense_and_marks_transaction_processed  # one test
pytest -k "guard"                                              # by keyword

# Run the app (RUN_MODE=polling for local dev, no public URL needed)
set -a && source .env && set +a
export RUN_MODE=polling
uvicorn app.main:app --reload --host 127.0.0.1 --port "${PORT:-7001}"

# Sanity-check the LLMProvider abstraction against a real model (not mocks)
python -m scripts.try_decide           # uses LLM_PROVIDER from .env
python -m scripts.try_decide --both    # Ollama + Claude side by side (needs ANTHROPIC_API_KEY)
```

## Code standards

Ruff (lint + format) and mypy are configured in `pyproject.toml`:

```bash
ruff check .          # lint
ruff format .         # format (format --check in CI)
mypy app              # type-check; scoped to app/ — alembic/scripts/tests are excluded
```

Both run as the `lint` job in CI, alongside the existing `test` job. `mypy`
is `disallow_untyped_defs` on `app/` — third-party stubs that mark things
Optional even though this app's own usage guarantees them non-None (e.g.
python-telegram-bot's `Update.effective_chat`/`Application.updater`) get a
narrow, commented override or `assert` rather than broadening the config;
see `pyproject.toml`'s `app.telegram.handlers.*` override and the asserts in
`app/agent.py`/`app/expenses.py`/`app/query.py`/`app/parser.py` for the
pattern.

## Architecture

### Routes vs. services

FastAPI routing (`app/routes.py`) and Telegram routing
(`app/telegram/handlers/`) are kept thin — they parse the incoming
request/Update, delegate to a service module, and format the reply/response.
Business logic lives in flat service modules alongside the pre-existing
ones, all directly under `app/` (no `routes/`/`services/` subpackages — kept
consistent with how `app/agent.py`/`app/budgets.py`/`app/query.py`/
`app/reports.py`/`app/parser.py` already worked before this split):
`app/cron.py` (the all-users cron bodies), `app/expenses.py` (creating an
`expenses` row directly — manual `/log`, resolving an ask_user answer, or the
audit's `recategorize_expense`), `app/ingestion.py` (turning an SMS or receipt
photo into a `transactions` row — or, for a credit, an `incomes` row). Phase 4
adds three more flat service modules alongside these: `app/income.py` (the
`incomes` ledger + `/income` summary), `app/salary.py` (the per-user
`salary_profiles` config + `/salary`), and `app/audit.py` (the monthly audit
agent + mid-month alerts). `app/main.py` is just the FastAPI app factory +
lifespan (Telegram `Application` startup/shutdown) + `include_router`.

### The provider boundary is load-bearing

`app/agent.py` and `app/parser.py` import only `app.llm.base` (the
`LLMProvider` Protocol + `LLMDecisionError`) and `app.schemas` — never
`anthropic`, `httpx`, or `google-genai` directly, and never
`app.llm.ollama` / `app.llm.claude` / `app.llm.gemini`.
`app.llm.factory.get_provider()` is the only place that picks a concrete
implementation, based on `settings.llm_provider`. Three interchangeable
providers exist — Ollama (free, local, dev default), Claude (paid, cloud),
Gemini (free tier, cloud — the recommended option for a deployment that
needs to run somewhere other than your own laptop; use `-latest` model
aliases like `gemini-flash-lite-latest` rather than a pinned dated version,
since Google retires those for new API keys) — and this boundary is what
makes the swap safe and keeps `tests/test_agent.py` and
`tests/test_parser.py` provider-agnostic (they drive a scripted
`FakeProvider` instead). All three real providers are constrained to the
*same* JSON Schema (generated from `app.schemas`, sanitized via
`sanitize_schema_for_llm` to strip keywords like `minimum`/`maximum` that
structured-output modes don't reliably support) and return the same
Pydantic model — `tests/test_llm_providers.py`,
`tests/test_agent_provider_parity.py`, and `tests/test_audit_provider_parity.py`
assert byte-for-byte equal output across all three providers given
identical mocked model responses, for every `LLMProvider` method
(`decide_match`, `parse_transaction`, `extract_receipt`, `interpret_query`,
`audit_finances`).

### The decision loop enforces its own conservatism — it doesn't trust the prompt

`app/agent.py:reconcile_user()` is perceive (load pending transactions) →
compare (`_find_candidates`, up to 3 unlinked expenses within
`candidate_amount_tolerance_pct`/`candidate_date_window_days`) → decide
(`provider.decide_match`) → act (`_act`). The system prompt tells the model
to be conservative, but `_apply_guard()` re-checks this in code: any
`auto_link`/`auto_log` decision with `confidence < settings.confidence_threshold`,
or an `auto_link` naming a `matched_expense_id` that wasn't actually in the
candidates offered (treated as a hallucination), gets rewritten to
`ask_user` regardless of what the model said. This matters because the dev
model (a local 7B) will not reliably obey a prompt-only rule. Commits happen
per-transaction (not per-batch) so one bad decision/exception can't roll
back the rest of a run — `reconcile_user` catches `LLMDecisionError` per
transaction and keeps going, counting it in `RunSummary.errors`.

Note two *different* confidence thresholds exist and happen to share the
value 0.75: `app.parser._LLM_FALLBACK_THRESHOLD` (regex-parse confidence,
gates whether the SMS parser calls the LLM at all) and
`settings.confidence_threshold` (the agent's own guard, above). They are
independent knobs — don't conflate them when tuning one. A third,
unrelated knob shares a value the same way: `settings.duplicate_charge_window_days`
(below) also defaults to 2, same as `candidate_date_window_days`, but governs
a different check entirely.

`reconcile_user`'s per-transaction body is actually two branches, not one
straight decide→guard pipeline:

- **Merchant-memory short-circuit.** If there are no `_find_candidates`
  results, no `txn.category_hint`, and `app.merchant_memory.recall_merchant_category`
  finds a remembered category for `txn.merchant` (learned from a past
  `ask_user` answer — see `app/expenses.py:resolve_ask_user_answer` below),
  the LLM is never called at all: a synthetic `auto_log` `MatchDecision` is
  built directly (confidence 1.0, the remembered category) and only passes
  through `_apply_duplicate_guard`, **not** `_apply_guard`/`_apply_category_hint`/
  `_normalize_auto_log_category`. This is deliberate, not an oversight: those
  three exist to second-guess a *model's* output (hallucination checks,
  casing normalization, "does this look like a real category") and a
  memory-sourced decision is already canonical and fully trusted — worse,
  running it through `_apply_guard` would immediately downgrade any
  caption-less **photo** repeat straight back to `ask_user` (its photo rule
  is unconditional on *why* `auto_log` was chosen), silently defeating the
  feature for photos. Candidates existing, or a `category_hint` already
  present, both skip the short-circuit — a real ledger match or this
  transaction's own explicit hint always outrank a historical default.
- **Normal path** (candidates exist, or no memory, or a hint is present):
  unchanged — `provider.decide_match` → `_apply_guard` →
  `_apply_duplicate_guard` → `_apply_category_hint` → `_normalize_auto_log_category`.

**`_apply_duplicate_guard`** is a second, independent guard (not a
`_apply_guard` branch) that runs in both branches above: it downgrades
`auto_link`/`auto_log` to `ask_user`, regardless of confidence, whenever
`_find_recent_duplicate_expense` finds an **existing** expense (linked or
not — unlike `_find_candidates`, which only ever offers unlinked ones) with
the same merchant and amount within `duplicate_charge_window_days`. A
same-merchant, same-amount repeat is often a double-debit, not two separate
purchases; this is a deliberate friction cost on legitimate same-day repeat
purchases in exchange for catching that.

Merchant memory is only ever *written* from an `ask_user` resolution
(`app/expenses.py:resolve_ask_user_answer`, atomic with its existing single
`session.commit()`), never from the LLM's own `auto_log` guess — the point
is to learn from the user's explicit choice, not from a possibly-wrong model
guess.

### The salary audit is a second agent loop — same conservatism, monthly grain

`app/audit.py:run_monthly_audit()` is the Phase 4 analog of `reconcile_user`,
but it runs once a month over a whole month rather than per-transaction:
perceive (`_load_month` — the month's incomes/expenses, the previous month for
deltas, budgets, salary profile) → compute (`build_snapshot` → a deterministic
`FinancialSnapshot`) → decide (`provider.audit_finances(snapshot)` →
`AuditReport`) → act (write the `audit_runs` trail, send the report + anomaly
questions). It reuses `reconcile_user`'s exact discipline:

- **The LLM writes prose, never numbers.** Every figure (income, spend, savings
  rate, category deltas) is computed in `build_snapshot` and formatted in
  `_format_message`. `audit_finances` gets the pre-computed snapshot and returns
  *only* `summary`, `recommendations`, and `flagged_expense_ids` — the same
  "anything that must be exactly right is computed, not phrased by a model"
  rule as `app/query.py` and `_apply_guard`.
- **`_apply_audit_guard` is the hallucination check**, mirroring `_apply_guard`:
  it drops any `flagged_expense_id` the model returns that wasn't among the
  `anomaly_candidates` `build_snapshot` actually offered.
- **The LLM is best-effort for the prose.** If `audit_finances` raises
  `LLMDecisionError`, the numbers still go out (report with no summary) — same
  resilience as `parse_sms` falling back to its regex result.
- **`audit_runs` is both the trail and the monthly idempotency guard.** Its
  unique `(user_id, period_month)` index means a re-run (or the on-demand
  `/audit` command hitting the same month) returns `already_audited` without a
  second LLM call — the same insert-guards-the-send shape as `budget_alerts_sent`.

Income is a *separate concern from expenses on purpose*: a credit SMS is written
straight to the `incomes` ledger in `app/ingestion.py` (not a `transactions`
row) and never enters `reconcile_user` — there's no manual income ledger to
reconcile it against. `/spend` and budgets sum `expenses` only, so income never
leaks into them.

The anomaly ask-flow reuses the inline-button machinery but with a **distinct
`acat:` callback** (`handle_audit_category_callback`) separate from reconcile's
`cat:` (`handle_category_callback`): `acat:` *recategorizes an existing expense*
in place (`expenses.recategorize_expense`), whereas `cat:` *creates* an expense
from a pending transaction (`expenses.resolve_ask_user_answer`). Two callback
patterns, two handlers, registered separately in `app/telegram/bot.py`.

`app/audit.py:check_midmonth_alerts()` is the deterministic (no-LLM) mid-month
counterpart — `salary_late` and `pace_high` — deduped via `audit_alerts_sent`
exactly like `check_budget_alerts` uses `budget_alerts_sent`. It returns `[]`
when the user has no `salary_profiles` row, since there's nothing to compare
actual income/spend against. Both `run_audit_all_users` (monthly) and
`check_salary_alerts_all_users` (daily) are cron bodies in `app/cron.py` behind
the `/run-audit` and `/check-salary-alerts` endpoints — same fail-closed
`CRON_SECRET` gate and 202-background-task shape as `/reconcile`/`/check-budgets`.

### `Savings` is a category, not a ledger — one flag, four call sites

`app/categories.py:SAVINGS_CATEGORY`/`is_savings_category()` mark money moved
to another account (e.g. your own savings account) as *not actually spent* —
a self-transfer, not spend. There's no separate table or status column for
this; it's a single case-insensitive category-name check that `/spend`
(`app/reports.py`), the monthly audit's `total_spend`/outlier-baseline/
anomaly-candidate selection (`app/audit.py:build_snapshot`), the mid-month
`pace_high` projection, and `/ask`'s `aggregation="net"` query
(`app/query.py:_run_net_query`) all apply independently — grep
`is_savings_category`/`SAVINGS_CATEGORY` before changing any one of these,
since the exclusion has to stay consistent across all four or a self-transfer
starts looking like overspending in one place but not another. The excluded
amount isn't just dropped, though: it's surfaced back out separately as
`moved_to_savings` in the audit snapshot and `net_moved_to_savings` in
`QueryResult`, so the user still sees where that money went.

`aggregation="net"` (added after the original four `/ask` aggregations) is
the only one that reads `incomes` as well as `expenses` — it answers "how
much money do I have left" style questions and is well-defined even with zero
activity (`"Rs.0.00 left"`), unlike `sum`/`count`/`max`/`list`, which fall
back to "No expenses found" on an empty result.

### Table state machine

Core tables (`app/models.py`): `users` (bridges Telegram `chat_id` → an
internal UUID — not in the original spec, added so the ledger isn't coupled
to Telegram directly), `transactions`, `expenses` (the actual ledger — `/spend`
in `app/reports.py` sums this, not `transactions`), `reconciliation_runs`
(the audit trail). `transactions.status` (`pending`|`processed`) and
`reconciliation_runs.status` (`open`|`resolved`) are what let the agent loop
and the Telegram callback handler find work without a fragile join:
`auto_link`/`auto_log` immediately mark the transaction `processed` and the
run `resolved`; `ask_user` leaves the transaction `pending` and the run
`open` until `app/telegram/handlers/callbacks.py:handle_category_callback`
resolves it, via `app/expenses.py:resolve_ask_user_answer` (creates the
`expenses` row with `created_via='manual'`, flips both statuses); the
handler then edits the Telegram message to drop the buttons.

Phase 3a added `budgets` + `budget_alerts_sent` (per-category monthly limit +
once-per-month alert dedup). Phase 4 added four more: `incomes` (the money-in
ledger — deliberately *not* `expenses`, so `/spend`/budgets never see it),
`salary_profiles` (one row per user: expected salary, savings target, payday),
`audit_runs` (the monthly-audit trail, whose unique `(user_id, period_month)`
index doubles as the once-per-month idempotency guard), and `audit_alerts_sent`
(mid-month alert dedup, keyed `(user_id, month, alert_type)` — the
`budget_alerts_sent` pattern). No status columns on the Phase 4 tables: the
audit's "have I already done this month" question is answered by row existence
in `audit_runs`/`audit_alerts_sent`, not a status field.

`merchant_categories` (model `MerchantCategory`) is a different shape again —
not a dedup table, and not per-month: one row per `(user_id, merchant)`
(unique index, `merchant` always pre-normalized via
`app.merchant_memory.normalize_merchant` before it's written or queried), a
plain key-value memory that a later `ask_user` answer for the same merchant
*overwrites* rather than appends to. See "The decision loop enforces its own
conservatism" above for how it's read, and `app/expenses.py:resolve_ask_user_answer`
for where it's written.

### SMS parsing: regex-first, direction-aware, LLM fallback for the residue

`app/parser.py:parse_sms()` scores confidence as amount(0.5) + date(0.25) +
merchant(0.25); below `_LLM_FALLBACK_THRESHOLD` (0.75) it calls
`provider.parse_transaction()` instead of guessing. Merchant extraction is
direction-aware (`_parse_merchant(text, is_debit)`): a debit message prefers
"to/towards/VPA `<recipient>`" and only falls back to "from
`<counterparty>`" if that's absent, while a credit message tries the reverse
first. This split exists because a debit SMS shape like "Sent Rs.90 **From**
HDFC Bank A/C \*9457 **To** MOHAMMED..." has "From" naming the *user's own*
account, not the counterparty — a single shared preposition pattern picks
whichever appears first in the text rather than the one that's actually the
counterparty. Direction must therefore be parsed before merchant in
`parse_sms`.

### Two independent /reconcile entry points

`app/telegram/handlers/commands.py:handle_reconcile_command` (the
`/reconcile` Telegram command) reconciles only the requesting chat's user.
`app/routes.py:reconcile_endpoint` (`POST /reconcile`, for the external
weekly cron) has no Telegram chat context, so it schedules
`app/cron.py:reconcile_all_users` via `BackgroundTasks` instead, which loops
over *every* user in the DB, one DB session per user, and returns `202`
immediately — the actual loop (and each user's summary message) happens
after the response is sent.

### Every non-/health route fails closed on its shared secret

`app/routes.py` gates `/telegram/webhook` on `settings.telegram_webhook_secret`
(against `X-Telegram-Bot-Api-Secret-Token`) and `/reconcile`/`/check-budgets`
on `settings.cron_secret` (against `X-Cron-Secret`, via the
`_verify_cron_secret` dependency). Both checks are written as "reject unless
the secret is set *and* matches" — never "only check if a secret happens to
be configured". An unset secret must reject every caller, not admit them:
the earlier version of the webhook check treated a missing
`telegram_webhook_secret` as "no check needed", which meant forgetting to
set it in production silently accepted forged Telegram updates rather than
failing loudly. If you touch either check, preserve the fail-closed shape —
"missing config" and "wrong secret" must be indistinguishable from the
caller's side (both 401), and neither `/reconcile` nor `/check-budgets`
should ever run without `CRON_SECRET` configured, since they trigger an LLM
run for every user in the database.

### Logging requires explicit setup — this bit us once already

Uvicorn only configures its own `uvicorn`/`uvicorn.access`/`uvicorn.error`
loggers (each `propagate=False`); it never attaches a handler to the root
logger. Any `logging.getLogger(__name__).info(...)` call anywhere in `app/*`
silently disappears unless `app.logging_config.configure_logging()` has run
first (it's called at module level in `app/main.py`, `force=True`, before
anything else logs). If you add a new module that logs and nothing shows up,
this is almost certainly why — don't add a competing `logging.basicConfig()`
elsewhere.

### Neon/asyncpg connection gotchas (already handled, don't re-break)

`app/db.py` passes `statement_cache_size=0` / `prepared_statement_cache_size=0`
in `connect_args` — required because Neon's pooled (pgbouncer, transaction-mode)
endpoint breaks asyncpg's server-side prepared-statement cache otherwise.
`DATABASE_URL` must use `postgresql+asyncpg://` (not bare `postgresql://`)
and `?ssl=require` (not Neon's default `?sslmode=require&channel_binding=require` —
asyncpg doesn't understand `channel_binding` at all). `alembic/env.py` derives
`sqlalchemy.url` from `app.config.settings` rather than `alembic.ini`, and
only applies the asyncpg `connect_args` when the URL actually contains
`+asyncpg` (so tests can point it at `sqlite+aiosqlite` instead).
