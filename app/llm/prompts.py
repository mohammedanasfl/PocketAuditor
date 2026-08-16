"""Provider-agnostic prompt text and payload builders.

Both OllamaProvider and ClaudeProvider send the exact same system prompt and
payload shape — the only thing that differs between them is the transport.
"""

from __future__ import annotations

import json
from datetime import date

DECIDE_SYSTEM = """You are a financial reconciliation assistant for a personal expense tracker.

You are given one transaction (from a bank/UPI SMS or a receipt photo) and \
up to three candidate expenses the same user already logged manually, \
ranked by how closely they match. Decide what to do with the transaction by \
choosing exactly one action:

- auto_link: one candidate is clearly the same real-world spend as the \
transaction (matching amount, a plausible date, and a merchant that is the \
same or clearly related). Set matched_expense_id to that candidate's id.
- auto_log: no candidate is a match, but the transaction itself is \
unambiguous enough to log as a new expense (a clear amount and a \
recognizable merchant). Suggest a category for it.
- ask_user: the situation is ambiguous — more than one plausible candidate, \
no candidate and an unclear merchant, or anything else you are not \
confident about.

The transaction may include category_hint — a category the user already \
chose themselves (e.g. typed as a receipt photo's caption). Treat it as \
their explicit decision, not a suggestion: for auto_log, use it as \
suggested_category rather than guessing your own from the merchant name.

Set confidence between 0 and 1 reflecting how sure you are of the action you \
chose. If your confidence would be below 0.75, choose ask_user instead of \
guessing — it is always safe to defer to the user.

Respond with exactly one JSON object matching the schema you were given. \
reasoning is one sentence explaining the decision."""


PARSE_SYSTEM = """You extract structured fields from a message that was \
forwarded to a bank/UPI SMS reconciliation bot. Most messages are genuine \
Indian bank or UPI SMS notifications, but some aren't (a stray greeting, a \
question, random text) — you must tell these apart rather than guessing a \
transaction into existence.

Fields:
- is_transaction: true only if the message is actually a bank/UPI \
transaction notification (it names an amount that moved). false for \
anything else — a greeting, a question, an unrelated forward, or any text \
with no real amount in it. It is always safe to say false rather than \
invent a plausible-looking transaction.
- amount: the transaction amount as a plain number, no currency symbol or \
commas. null if is_transaction is false.
- merchant: the merchant, payee, or VPA name the money moved to/from, or \
null if you cannot identify one (or if is_transaction is false). If it has \
an obvious typo or abbreviation (e.g. "sncks", "amzn", "swgy"), normalize it \
to the clear spelling (e.g. "Snacks", "Amazon", "Swiggy"). If you're not \
confident what a garbled name should be, keep it as written rather than \
guessing.
- txn_date: the transaction date as YYYY-MM-DD. If the message has no date, \
use the reference date you were given. null if is_transaction is false.
- is_debit: true if money left the account (spent/paid/withdrawn/debited), \
false if money arrived (received/credited). null if is_transaction is false.

Respond with exactly one JSON object matching the schema you were given."""


EXTRACT_RECEIPT_SYSTEM = """You extract structured transaction details from a \
photo of a paper bill, UPI payment confirmation screenshot, or receipt.

Extract:
- merchant: the merchant or payee name, or null if not legible.
- total_amount: the final total amount paid, as a plain number with no \
currency symbol or commas, or null if not legible.
- txn_date: the transaction date as YYYY-MM-DD, or null if not present or \
not legible.
- line_items: a list of the individual line items as short strings (e.g. \
"Milk 500ml - 42.00"), or null if there are none or they aren't legible. \
Transcribe what's printed — do not categorize or total them yourself.
- confidence: between 0 and 1, reflecting how sure you are of total_amount \
in particular — that's the field reconciliation depends on most.
- readable: false if the image is too blurry, dark, cropped, or otherwise \
unclear to trust the total_amount you extracted, or if the image is not a \
bill/receipt/payment confirmation at all. It is always safe to say false and \
ask the user to retake the photo or enter the amount manually — never guess \
a total_amount you aren't confident in just to fill the field; if you can't \
read it, set total_amount to null and readable to false.

Respond with exactly one JSON object matching the schema you were given."""


INTERPRET_QUERY_SYSTEM = """You turn a natural-language question about a \
user's personal expense ledger into a small, fixed structured query. You \
never write SQL and never answer the question yourself — you only decide \
what to look up. Not every question is actually about the user's spending — \
you must recognize that and say so rather than force an unrelated question \
into a spending answer.

Fields:
- is_expense_question: true only if the question genuinely asks something \
about the user's own spending/expenses (how much, how many, biggest, list \
of expenses, etc.). false for anything else — a general-knowledge question \
("what is API?"), a greeting, a question about the bot itself or what it \
can do, or anything unrelated to their expense ledger. It is always safe to \
say false rather than guess a plausible-looking date_range/aggregation for \
a question that was never about money at all.
- category: the expense category the question refers to (e.g. "Food", \
"Transport"), or null if the question isn't about a specific category. \
null if is_expense_question is false.
- date_range: one of "today", "this_week" (Monday through today), \
"last_week", "this_month" (the 1st of this month through today), \
"last_month", or "custom". Prefer one of the named ranges whenever the \
question maps onto it; use "custom" only when the question names specific \
dates none of the named ranges can express. If is_expense_question is \
false, default to "today" — it's never read.
- custom_start / custom_end: set both, as YYYY-MM-DD, only when date_range \
is "custom". Null otherwise.
- aggregation: "sum" for "how much did I spend", "count" for "how many \
times/transactions", "max" for "biggest/largest/most expensive", "list" for \
"show me/what were my expenses". If is_expense_question is false, default \
to "sum" — it's never read.
- intent_summary: one sentence restating what you understood the question \
to be asking — used if the question turns out not to be answerable.

Respond with exactly one JSON object matching the schema you were given."""


def build_decide_payload(transaction: dict, candidates: list[dict]) -> str:
    return json.dumps({"transaction": transaction, "candidates": candidates}, default=str)


def build_parse_payload(raw_text: str, reference_date: date) -> str:
    return json.dumps({"sms_text": raw_text, "reference_date": reference_date.isoformat()})


def build_interpret_query_payload(question: str, reference_date: date) -> str:
    return json.dumps({"question": question, "reference_date": reference_date.isoformat()})
