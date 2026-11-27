# Evals

Scenario files in `scenarios/` describe an input and the correct outcome.
`run.py` runs them and writes a report to `results/`.

```bash
# offline — measures the plain code (regex parser, guards, memory) with no LLM
python -m evals.run --suite parser --provider none
python -m evals.run --suite reconcile --provider none

# with a real model (needs GEMINI_API_KEY / ANTHROPIC_API_KEY in .env)
python -m evals.run --suite parser --provider gemini --model gemini-3.5-flash-lite
python -m evals.run --suite reconcile --provider gemini --model gemini-3.5-flash-lite --engine pipeline
python -m evals.run --suite reconcile --provider gemini --model gemini-3.5-flash-lite --engine agent
```

## Metrics

| Metric | Meaning |
|---|---|
| Correct | outcome (and category/amount when given) matches the expected one |
| Unsafe | wrong money data would be saved: a non-transaction saved, wrong amount/direction, an expense written when the right answer was to ask, or the wrong category |
| Unnecessary asks | asked the user when it could have decided on its own (annoying, but safe) |
| LLM calls / time | cost and speed |

**Unsafe must be 0.** Asking too often is a usability problem; writing wrong
data is a correctness problem.

## Scenario format

`parser.jsonl` — one SMS, expected `is_transaction`, `is_debit`, `amount`,
and optionally a word the merchant must contain.

`reconcile.jsonl` — `setup` (existing `expenses`, merchant `memory`,
`own_accounts`), the `sms`, and `expected.outcome`: `auto_log` (with
`category`, optional `also_ok`), `auto_link`, `ask_user`, `rejected`,
`income`, `savings_transfer` or `savings_withdrawal`.
