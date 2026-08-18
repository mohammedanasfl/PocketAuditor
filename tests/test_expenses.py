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

from sqlalchemy import select

from app.expenses import recategorize_expense, resolve_ask_user_answer
from app.models import Expense, MerchantCategory, ReconciliationRun, Transaction, User


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


async def test_recategorize_expense_updates_category(db_session):
    user = await _make_user(db_session)
    expense = Expense(user_id=user.id, amount=Decimal("3000"), category="Uncategorized", txn_date=date(2026, 7, 9))
    db_session.add(expense)
    await db_session.commit()

    updated = await recategorize_expense(db_session, expense.id, "Shopping")

    assert updated is not None
    assert updated.category == "Shopping"
    await db_session.refresh(expense)
    assert expense.category == "Shopping"


async def test_recategorize_missing_expense_returns_none(db_session):
    assert await recategorize_expense(db_session, uuid4(), "Food") is None


async def test_resolving_ask_user_remembers_the_merchant_category(db_session):
    user = await _make_user(db_session)
    run = await _make_pending_run(db_session, user)

    await resolve_ask_user_answer(db_session, run.id, "Food")

    memory = (
        await db_session.execute(select(MerchantCategory).where(MerchantCategory.user_id == user.id))
    ).scalar_one()
    assert memory.merchant == "unknown shop"  # pre-normalized
    assert memory.category == "Food"


async def test_resolving_a_second_question_for_the_same_merchant_updates_the_memory(db_session):
    """A later ask_user answer overwrites the remembered category rather than
    creating a second row — the (user, merchant) unique index enforces this,
    and resolve_ask_user_answer's upsert must respect it."""
    user = await _make_user(db_session)
    first_run = await _make_pending_run(db_session, user)
    await resolve_ask_user_answer(db_session, first_run.id, "Food")

    second_run = await _make_pending_run(db_session, user)
    await resolve_ask_user_answer(db_session, second_run.id, "Shopping")

    stmt = select(MerchantCategory).where(MerchantCategory.user_id == user.id)
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert rows[0].category == "Shopping"


async def test_resolving_ask_user_with_no_merchant_does_not_write_memory(db_session):
    user = await _make_user(db_session)
    txn = Transaction(
        user_id=user.id,
        raw_text="Rs.500 debited",
        amount=Decimal("500.00"),
        merchant=None,
        txn_date=date(2026, 8, 10),
        source="sms",
        status="pending",
    )
    db_session.add(txn)
    await db_session.commit()
    run = ReconciliationRun(
        user_id=user.id,
        transaction_id=txn.id,
        decision="ask_user",
        reasoning="unfamiliar",
        confidence=Decimal("0.40"),
        status="open",
    )
    db_session.add(run)
    await db_session.commit()

    await resolve_ask_user_answer(db_session, run.id, "Food")

    assert (await db_session.execute(select(MerchantCategory))).scalars().all() == []
