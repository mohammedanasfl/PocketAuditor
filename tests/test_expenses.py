"""app.expenses.resolve_ask_user_answer — extracted from the Telegram
category-button callback (app/telegram/handlers/callbacks.py:
handle_category_callback), which previously had no dedicated test coverage
of its own. Covers the same three cases that callback has to distinguish:
answering an open question, a double-tap on an already-resolved one, and an
unknown run id.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.expenses import resolve_ask_user_answer
from app.models import ReconciliationRun, Transaction, User


async def _make_user(session) -> User:
    user = User(telegram_chat_id=11223)
    session.add(user)
    await session.commit()
    return user


async def _make_pending_run(session, user: User, *, status: str = "open") -> ReconciliationRun:
    txn = Transaction(
        user_id=user.id,
        raw_text="Rs.500 debited towards UNKNOWN SHOP",
        amount=Decimal("500.00"),
        merchant="UNKNOWN SHOP",
        txn_date=date(2026, 8, 10),
        source="sms",
        status="pending",
    )
    session.add(txn)
    await session.commit()

    run = ReconciliationRun(
        user_id=user.id,
        transaction_id=txn.id,
        decision="ask_user",
        reasoning="unfamiliar merchant",
        confidence=Decimal("0.40"),
        status=status,
    )
    session.add(run)
    await session.commit()
    return run


async def test_answering_an_open_question_creates_expense_and_resolves_run(db_session):
    user = await _make_user(db_session)
    run = await _make_pending_run(db_session, user)

    expense = await resolve_ask_user_answer(db_session, run.id, "Food")

    assert expense is not None
    assert expense.category == "Food"
    assert expense.amount == Decimal("500.00")
    assert expense.merchant == "UNKNOWN SHOP"
    assert expense.created_via == "manual"

    await db_session.refresh(run)
    txn = await db_session.get(Transaction, run.transaction_id)
    assert run.status == "resolved"
    assert run.resolved_at is not None
    assert txn is not None
    assert txn.status == "processed"


async def test_double_tap_on_already_resolved_run_returns_none(db_session):
    user = await _make_user(db_session)
    run = await _make_pending_run(db_session, user, status="resolved")

    expense = await resolve_ask_user_answer(db_session, run.id, "Food")

    assert expense is None


async def test_unknown_run_id_returns_none(db_session):
    expense = await resolve_ask_user_answer(db_session, uuid4(), "Food")

    assert expense is None
