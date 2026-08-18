"""Pydantic models shared by the LLM interface.

Both providers are constrained by JSON Schema generated from these same
models, and both return a value validated by that same model — that's what
makes "identical shape regardless of provider" enforceable by construction
rather than by convention.

Fields typed `X | None` with no Python-level default (e.g. `matched_expense_id`)
are deliberate: Pydantic v2 only treats a field as optional when it has an
explicit default, so omitting one keeps the field in the generated schema's
"required" list while the type itself stays nullable. That's the standard
"required-nullable-field" idiom structured-output schemas expect — the model
must always emit the key, using null when it doesn't apply.
"""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MatchDecision(BaseModel):
    """Output of LLMProvider.decide_match."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["auto_link", "auto_log", "ask_user"]
    matched_expense_id: UUID | None
    suggested_category: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class LLMExtraction(BaseModel):
    """Output of LLMProvider.parse_transaction — fields an LLM can pull out of
    a forwarded SMS body when the regex-first pass isn't confident enough.
    Combined with regex-derived confidence/method by app/parser.py (Stage 3).

    `is_transaction` exists for the same reason ExtractedReceipt.readable
    does: without an explicit escape hatch, a structured-output schema forces
    the model to fabricate a plausible-looking amount/date even for text that
    isn't a transaction at all (e.g. a stray "Help" message) — there's no way
    to say "there's nothing here" other than an explicit field for it.
    """

    model_config = ConfigDict(extra="forbid")

    is_transaction: bool
    amount: float | None
    merchant: str | None
    txn_date: date | None
    is_debit: bool | None


class ExtractedReceipt(BaseModel):
    """Output of LLMProvider.extract_receipt — Phase 2 photo capture. `readable`
    is the field the rest of Phase 2 trusts most: it must be false whenever the
    image is too blurry/dark/cropped to trust total_amount, or isn't a
    bill/receipt at all, rather than the model guessing a plausible number."""

    model_config = ConfigDict(extra="forbid")

    merchant: str | None
    total_amount: float | None
    txn_date: date | None
    line_items: list[str] | None
    confidence: float = Field(ge=0.0, le=1.0)
    readable: bool


class AuditReport(BaseModel):
    """Output of LLMProvider.audit_finances — Phase 4 monthly salary audit.

    The LLM is the auditor's *voice*, not its calculator. Every exact figure
    (income, spend, savings rate, category totals) is computed in app/audit.py
    and handed to the model in a snapshot; the model returns ONLY prose
    (summary + recommendations) and which of the pre-selected anomaly
    candidates to flag for review — never numbers. app/audit.py re-checks the
    flagged ids against the set it actually offered, in code, discarding any
    the model invented — the same "guard in code, don't trust the prompt"
    hallucination check app/agent.py's _apply_guard applies to
    matched_expense_id.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str
    recommendations: list[str]
    flagged_expense_ids: list[UUID]
    confidence: float = Field(ge=0.0, le=1.0)


class QueryIntent(BaseModel):
    """Output of LLMProvider.interpret_query — Phase 3b NL query chat, later
    widened (Phase 4) beyond pure spending questions to cover overall balance
    ("how much money do I have left") via aggregation="net".

    This is the *only* thing an LLM ever produces for a query: a small fixed
    intent, never SQL and never free text. app/query.py is the one place
    that turns a QueryIntent into a real, parameterized SQLAlchemy query —
    the LLM never sees or writes SQL, which is the actual safety property
    that matters for a personal-finance bot.

    `is_financial_question` exists for the same reason LLMExtraction.is_transaction
    and ExtractedReceipt.readable do: without an explicit escape hatch, the
    model is forced to squeeze an unrelated question (a general-knowledge
    question, a greeting, a question about the bot itself) into a financial
    answer that happens to look plausible — e.g. "what is API?" getting
    answered with a real total spend figure, because date_range/aggregation
    still have to be *something*.
    """

    model_config = ConfigDict(extra="forbid")

    is_financial_question: bool
    category: str | None
    date_range: Literal["today", "this_week", "last_week", "this_month", "last_month", "custom"]
    custom_start: date | None
    custom_end: date | None
    aggregation: Literal["sum", "count", "max", "list", "net"]
    intent_summary: str
