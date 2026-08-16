"""The agent's decision loop: perceive -> compare -> decide -> act.

Deliberately imports only app.llm.base / app.schemas for the LLM side and
app.models for the DB side — never `anthropic` or `httpx` directly — so the
provider swap and unit tests (a scripted FakeProvider against aiosqlite)
stay cheap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.base import LLMDecisionError, LLMProvider
from app.models import Expense, ReconciliationRun, Transaction
from app.schemas import MatchDecision

logger = logging.getLogger(__name__)

_MAX_CANDIDATES = 3


@dataclass
class PendingQuestion:
    """An ask_user decision that needs a Telegram message sent. Returned to
    the caller rather than sent directly from here — keeps Telegram concerns
    out of the agent loop entirely."""

    transaction: Transaction
    run_id: UUID
    suggested_category: str | None
    reasoning: str


@dataclass
class RunSummary:
    auto_linked: int = 0
    auto_logged: int = 0
    asked_user: int = 0
    errors: int = 0
    pending_questions: list[PendingQuestion] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.auto_linked + self.auto_logged + self.asked_user + self.errors

    def as_message(self) -> str:
        if self.total == 0:
            return "Nothing to reconcile — you're all caught up."
        parts = [
            f"{self.auto_linked} matched",
            f"{self.auto_logged} auto-logged",
            f"{self.asked_user} needs your input",
        ]
        if self.errors:
            parts.append(f"{self.errors} failed")
        return ", ".join(parts)


def _transaction_to_dict(txn: Transaction) -> dict:
    return {
        "amount": str(txn.amount),
        "merchant": txn.merchant,
        "txn_date": txn.txn_date.isoformat(),
        "raw_text": txn.raw_text,
        "category_hint": txn.category_hint,
    }


def _expense_to_dict(expense: Expense) -> dict:
    return {
        "id": str(expense.id),
        "amount": str(expense.amount),
        "category": expense.category,
        "merchant": expense.merchant,
        "txn_date": expense.txn_date.isoformat(),
        "notes": expense.notes,
    }


async def _find_candidates(session: AsyncSession, user_id: UUID, txn: Transaction) -> list[Expense]:
    """Up to 3 not-yet-linked expenses within an amount/date window, closest
    first. Excluding already-linked expenses is what stops one expense
    absorbing two transactions."""
    tolerance = txn.amount * Decimal(str(settings.candidate_amount_tolerance_pct))
    amount_low = txn.amount - tolerance
    amount_high = txn.amount + tolerance
    window = timedelta(days=settings.candidate_date_window_days)
    date_low = txn.txn_date - window
    date_high = txn.txn_date + window

    stmt = select(Expense).where(
        Expense.user_id == user_id,
        Expense.linked_transaction_id.is_(None),
        Expense.amount.between(amount_low, amount_high),
        Expense.txn_date.between(date_low, date_high),
    )
    result = await session.execute(stmt)
    candidates = list(result.scalars().all())

    def sort_key(expense: Expense) -> tuple[Decimal, int]:
        amount_delta = abs(expense.amount - txn.amount)
        date_delta = abs((expense.txn_date - txn.txn_date).days)
        return (amount_delta, date_delta)

    candidates.sort(key=sort_key)
    return candidates[:_MAX_CANDIDATES]


def _apply_guard(decision: MatchDecision, candidate_ids: set[str], txn: Transaction) -> MatchDecision:
    """Enforce conservatism in code, not just via the prompt — a 7B model
    will not reliably obey a prompt-only rule. Downgrades auto_link/auto_log
    to ask_user when confidence is below threshold, when the model names a
    matched_expense_id that wasn't actually among the candidates offered
    (treated as a hallucination), or when a photo-sourced transaction has no
    user-provided category_hint — a receipt photo with no caption and an
    unfamiliar merchant (e.g. a personal name from a P2P transfer) is exactly
    the ambiguous case that should ask rather than let the model guess a
    category. ask_user decisions pass through untouched — they're already
    the conservative choice."""
    if decision.action == "ask_user":
        return decision

    guard_note: str | None = None
    if decision.confidence < settings.confidence_threshold:
        guard_note = f"confidence {decision.confidence:.2f} below threshold {settings.confidence_threshold}"
    elif decision.action == "auto_link" and (
        decision.matched_expense_id is None or str(decision.matched_expense_id) not in candidate_ids
    ):
        guard_note = "matched_expense_id was not among the candidates offered"
    elif decision.action == "auto_log" and txn.source == "photo" and not txn.category_hint:
        guard_note = "photo transaction has no category hint — asking instead of guessing"

    if guard_note is None:
        return decision

    logger.info("guard fired: %s (was %s)", guard_note, decision.action)
    return MatchDecision(
        action="ask_user",
        matched_expense_id=None,
        suggested_category=decision.suggested_category,
        confidence=decision.confidence,
        reasoning=f"{decision.reasoning} [downgraded to ask_user: {guard_note}]",
    )


def _apply_category_hint(decision: MatchDecision, txn: Transaction) -> MatchDecision:
    """A category_hint is the user's explicit choice (e.g. from a receipt
    photo's caption) — trust it over whatever category the model guessed,
    same "guard in code, don't just hope the prompt used it" reasoning as
    _apply_guard. Only touches auto_log; auto_link uses the matched
    expense's own category, and ask_user is already deferring to the user."""
    if decision.action != "auto_log" or not txn.category_hint:
        return decision
    if decision.suggested_category == txn.category_hint:
        return decision
    return MatchDecision(
        action=decision.action,
        matched_expense_id=decision.matched_expense_id,
        suggested_category=txn.category_hint,
        confidence=decision.confidence,
        reasoning=decision.reasoning,
    )


async def _act(
    session: AsyncSession, user_id: UUID, txn: Transaction, decision: MatchDecision
) -> PendingQuestion | None:
    now = datetime.now(timezone.utc)

    if decision.action == "auto_link":
        expense = await session.get(Expense, decision.matched_expense_id)
        expense.linked_transaction_id = txn.id
        txn.status = "processed"
        run_status = "resolved"
        resolved_at = now
    elif decision.action == "auto_log":
        expense = Expense(
            user_id=user_id,
            amount=txn.amount,
            category=decision.suggested_category or "Uncategorized",
            merchant=txn.merchant,
            txn_date=txn.txn_date,
            linked_transaction_id=txn.id,
            created_via="auto_log",
        )
        session.add(expense)
        txn.status = "processed"
        run_status = "resolved"
        resolved_at = now
    else:  # ask_user — transaction stays 'pending' until the user answers
        run_status = "open"
        resolved_at = None

    run = ReconciliationRun(
        user_id=user_id,
        transaction_id=txn.id,
        decision=decision.action,
        reasoning=decision.reasoning,
        confidence=Decimal(str(round(decision.confidence, 2))),
        status=run_status,
        resolved_at=resolved_at,
    )
    session.add(run)
    await session.commit()  # per-transaction commit: one bad decision can't roll back the rest of the batch

    if decision.action == "ask_user":
        return PendingQuestion(
            transaction=txn,
            run_id=run.id,
            suggested_category=decision.suggested_category,
            reasoning=decision.reasoning,
        )
    return None


async def reconcile_user(session: AsyncSession, provider: LLMProvider, user_id: UUID) -> RunSummary:
    """Run the full perceive -> compare -> decide -> act loop over every
    pending transaction for one user. A single transaction's LLM failure is
    recorded and skipped rather than aborting the whole run."""
    summary = RunSummary()

    stmt = (
        select(Transaction)
        .where(Transaction.user_id == user_id, Transaction.status == "pending")
        .order_by(Transaction.created_at)
    )
    result = await session.execute(stmt)
    transactions = list(result.scalars().all())
    logger.info("reconcile_user: user=%s — %d pending transaction(s)", user_id, len(transactions))

    for txn in transactions:
        candidates = await _find_candidates(session, user_id, txn)
        candidate_dicts = [_expense_to_dict(expense) for expense in candidates]
        candidate_ids = {c["id"] for c in candidate_dicts}

        try:
            decision = await provider.decide_match(_transaction_to_dict(txn), candidate_dicts)
        except LLMDecisionError as exc:
            logger.warning("txn=%s: provider error, skipping (%s)", txn.id, exc)
            summary.errors += 1
            continue

        decision = _apply_guard(decision, candidate_ids, txn)
        decision = _apply_category_hint(decision, txn)
        pending = await _act(session, user_id, txn, decision)
        logger.info(
            "txn=%s: decision=%s confidence=%.2f candidates=%d",
            txn.id, decision.action, decision.confidence, len(candidate_dicts),
        )

        if decision.action == "auto_link":
            summary.auto_linked += 1
        elif decision.action == "auto_log":
            summary.auto_logged += 1
        else:
            summary.asked_user += 1
            if pending is not None:
                summary.pending_questions.append(pending)

    logger.info("reconcile_user: user=%s finished — %s", user_id, summary.as_message())
    return summary
