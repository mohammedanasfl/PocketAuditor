"""Expense-side service functions: creating a ledger row directly (no
underlying transaction, e.g. /log) and resolving an ask_user question into
one (the inline-button category answer).

Kept separate from app.ingestion, which only ever creates `transactions` —
nothing in there writes to `expenses` directly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.merchant_memory import remember_merchant_category
from app.models import Expense, ReconciliationRun, Transaction
from app.timeutil import local_today

logger = logging.getLogger(__name__)


async def log_manual_expense(
    session: AsyncSession, user_id: UUID, category: str, amount: Decimal, notes: str | None
) -> Expense:
    """/log <category> <amount> [notes] — no bank/UPI transaction behind this
    at all, so there's nothing to reconcile against; the Expense is created
    directly, same created_via='manual' as answering an ask_user question."""
    expense = Expense(
        user_id=user_id,
        amount=amount,
        category=category,
        merchant=None,
        notes=notes,
        txn_date=local_today(),
        created_via="manual",
    )
    session.add(expense)
    await session.commit()
    logger.info("logged manual expense=%s user=%s amount=%s category=%s", expense.id, user_id, amount, category)
    return expense


async def resolve_ask_user_answer(
    session: AsyncSession, run_id: UUID, category: str, *, user_id: UUID
) -> Expense | None:
    """Resolves the inline-button answer to an ask_user question: creates the
    Expense, links it back to the transaction, and flips both the run and
    transaction to their resolved states. Returns None if the run is already
    resolved (or doesn't exist) rather than raising, since a double-tap on
    an already-answered Telegram message is an expected race, not an error.

    `user_id` is the user who tapped the button: a run belonging to anyone
    else is treated as not found, so nobody can answer another user's
    question."""
    # Claim the question in ONE statement, so a double tap on two buttons
    # can't create two expenses.
    claim = await session.execute(
        update(ReconciliationRun)
        .where(ReconciliationRun.id == run_id, ReconciliationRun.user_id == user_id, ReconciliationRun.status == "open")
        .values(status="resolved", resolved_at=datetime.now(UTC))
    )
    if claim.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return None
    run = await session.get(ReconciliationRun, run_id)
    assert run is not None
    await session.refresh(run)

    txn = await session.get(Transaction, run.transaction_id)
    # A ReconciliationRun always references a real Transaction (foreign key,
    # never deleted independently) — guaranteed to resolve here.
    assert txn is not None

    expense = Expense(
        user_id=run.user_id,
        amount=txn.amount,
        category=category,
        merchant=txn.merchant,
        txn_date=txn.txn_date,
        linked_transaction_id=txn.id,
        created_via="manual",
    )
    session.add(expense)

    txn.status = "processed"

    await remember_merchant_category(session, run.user_id, txn.merchant, category)

    await session.commit()
    logger.info("run=%s: resolved via ask_user answer, category=%s expense=%s", run_id, category, expense.id)
    return expense


async def recategorize_expense(
    session: AsyncSession, expense_id: UUID, category: str, *, user_id: UUID
) -> Expense | None:
    """Change an existing expense's category — the monthly audit's anomaly
    ask-flow (the `acat:` inline buttons). Distinct from resolve_ask_user_answer,
    which *creates* an expense from a pending transaction: here the expense
    already exists (auto_logged or manual) and only its category changes.
    Returns None if the expense no longer exists (an expected stale-button
    race, not an error). An expense belonging to another user is treated the
    same as a missing one."""
    expense = await session.get(Expense, expense_id)
    if expense is None or expense.user_id != user_id:
        return None
    expense.category = category
    await session.commit()
    logger.info("recategorized expense=%s to %s", expense_id, category)
    return expense
