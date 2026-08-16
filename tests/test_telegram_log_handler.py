"""The /log command handler: manually logging an expense with no underlying
bank/UPI transaction at all (e.g. physical cash spend, no bank alert ever
arrives for that). Unlike SMS/photo, this creates an Expense directly —
there's nothing to reconcile against.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.telegram.handlers.commands as handlers_module
from app.db import Base
from app.models import Expense
from app.telegram.handlers import handle_log_command


def _make_update_and_context(args: list[str]):
    replies: list[str] = []

    async def reply_text(text: str) -> None:
        replies.append(text)

    message = SimpleNamespace(reply_text=reply_text)
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=42))
    context = SimpleNamespace(args=args)
    return update, context, replies


async def _sqlite_session_factory(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'log.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(handlers_module, "SessionLocal", session_factory)
    return session_factory, engine


async def test_log_creates_an_expense_with_no_transaction(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["Food", "900"])

    await handle_log_command(update, context)

    async with session_factory() as session:
        expenses = (await session.execute(select(Expense))).scalars().all()
    assert len(expenses) == 1
    expense = expenses[0]
    assert expense.category == "Food"
    assert expense.amount == Decimal("900")
    assert expense.created_via == "manual"
    assert expense.linked_transaction_id is None
    assert expense.txn_date == date.today()
    assert len(replies) == 1 and "900.00" in replies[0] and "Food" in replies[0]
    await engine.dispose()


async def test_log_stores_optional_notes(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["Food", "900", "lunch", "with", "friends"])

    await handle_log_command(update, context)

    async with session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
    assert expense.notes == "lunch with friends"
    assert "lunch with friends" in replies[0]
    await engine.dispose()


async def test_log_is_case_insensitive_against_known_categories(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["food", "900"])

    await handle_log_command(update, context)

    async with session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
    assert expense.category == "Food"  # normalized to canonical casing
    await engine.dispose()


async def test_log_defaults_unrecognized_category_to_other(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["Crypto", "900"])

    await handle_log_command(update, context)

    async with session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
    assert expense.category == "Other"
    assert expense.amount == Decimal("900")
    assert len(replies) == 1
    assert "Other" in replies[0]
    assert "Crypto" in replies[0]  # fallback is called out, not silent
    await engine.dispose()


async def test_log_rejects_non_positive_amount(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["Food", "-50"])

    await handle_log_command(update, context)

    async with session_factory() as session:
        assert (await session.execute(select(Expense))).scalars().all() == []
    assert len(replies) == 1
    await engine.dispose()


async def test_log_rejects_malformed_amount(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["Food", "lots"])

    await handle_log_command(update, context)

    async with session_factory() as session:
        assert (await session.execute(select(Expense))).scalars().all() == []
    assert len(replies) == 1
    await engine.dispose()


async def test_log_shows_usage_when_missing_args(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["Food"])

    await handle_log_command(update, context)

    assert len(replies) == 1 and "Usage" in replies[0]
    await engine.dispose()
