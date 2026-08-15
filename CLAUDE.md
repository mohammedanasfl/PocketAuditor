# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PocketAuditor Phase 1: a Telegram bot that reconciles forwarded bank/UPI SMS
against a manually-kept expense ledger. For each unprocessed transaction, an
LLM-backed agent decides `auto_link` (match an existing expense), `auto_log`
(create a new expense on its own), or `ask_user` (ask via inline buttons).
Every decision is written to `reconciliation_runs` for audit.

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
uvicorn app.main:app --reload --host 127.0.0.1 --port "${PORT:-8000}"

# Sanity-check the LLMProvider abstraction against a real model (not mocks)
python -m scripts.try_decide           # uses LLM_PROVIDER from .env
python -m scripts.try_decide --both    # Ollama + Claude side by side (needs ANTHROPIC_API_KEY)
```

There is no linter/formatter configured in this repo.

## Architecture

### The provider boundary is load-bearing

`app/agent.py` and `app/parser.py` import only `app.llm.base` (the
`LLMProvider` Protocol + `LLMDecisionError`) and `app.schemas` — never
`anthropic` or `httpx` directly, and never `app.llm.ollama` /
`app.llm.claude`. `app.llm.factory.get_provider()` is the only place that
picks a concrete implementation, based on `settings.llm_provider`. This is
what makes the dev (Ollama) / prod (Claude) swap safe and keeps
`tests/test_agent.py` and `tests/test_parser.py` provider-agnostic (they
drive a scripted `FakeProvider` instead). Both real providers are
constrained to the *same* JSON Schema (generated from `app.schemas`,
sanitized via `sanitize_schema_for_llm` to strip keywords like `minimum`/
`maximum` that structured-output modes don't reliably support) and return
the same Pydantic model — `tests/test_llm_providers.py` and
`tests/test_agent_provider_parity.py` assert byte-for-byte equal output from
both providers given identical mocked model responses.

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
independent knobs — don't conflate them when tuning one.

### Table state machine

Four tables (`app/models.py`): `users` (bridges Telegram `chat_id` → an
internal UUID — not in the original spec, added so the ledger isn't coupled
to Telegram directly), `transactions`, `expenses` (the actual ledger — `/spend`
in `app/reports.py` sums this, not `transactions`), `reconciliation_runs`
(the audit trail). `transactions.status` (`pending`|`processed`) and
`reconciliation_runs.status` (`open`|`resolved`) are what let the agent loop
and the Telegram callback handler find work without a fragile join:
`auto_link`/`auto_log` immediately mark the transaction `processed` and the
run `resolved`; `ask_user` leaves the transaction `pending` and the run
`open` until `app/telegram/handlers.py:handle_category_callback` resolves it
(creates the `expenses` row with `created_via='manual'`, flips both statuses,
edits the Telegram message to drop the buttons).

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

`app/telegram/handlers.py:handle_reconcile_command` (the `/reconcile`
Telegram command) reconciles only the requesting chat's user.
`app/main.py:reconcile_endpoint` (`POST /reconcile`, for the external weekly
cron) has no Telegram chat context, so `_run_reconcile_all_users` instead
loops over *every* user in the DB, one DB session per user, and returns
`202` immediately via `BackgroundTasks` — the actual loop (and each user's
summary message) happens after the response is sent.

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
