"""Phase 4: the acat: inline-button callback (handle_audit_category_callback),
which recategorizes an existing expense flagged by the monthly audit. Fakes the
Telegram CallbackQuery with SimpleNamespace and points the callbacks module's
SessionLocal at a fresh sqlite DB.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.telegram.handlers.callbacks as callbacks_module
from app.db import Base
from app.models import Expense, User
from app.telegram.handlers import handle_audit_category_callback


def _make_callback(data: str):
    edits: list[str] = []
    answers: list[None] = []

    async def answer() -> None:
        answers.append(None)

    async def edit_message_text(text: str) -> None:
        edits.append(text)

    query = SimpleNamespace(data=data, answer=answer, edit_message_text=edit_message_text)
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=42))
    context = SimpleNamespace()
    return update, context, edits, answers


async def _sqlite_session_factory(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'acb.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(callbacks_module, "SessionLocal", session_factory)
    return session_factory, engine


async def test_acat_callback_recategorizes_the_expense(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)

    async with session_factory() as session:
        user = User(telegram_chat_id=42)
        session.add(user)
        await session.commit()
        expense = Expense(user_id=user.id, amount=Decimal("3000"), category="Uncategorized", txn_date=date(2026, 7, 9))
        session.add(expense)
        await session.commit()
        expense_id = expense.id

    update, context, edits, answers = _make_callback(f"acat:{expense_id}:shopping")
    await handle_audit_category_callback(update, context)

    assert answers  # query.answer() was called
    assert edits == ["Recategorized as Shopping."]

    async with session_factory() as session:
        refreshed = await session.get(Expense, expense_id)
        assert refreshed is not None
        assert refreshed.category == "Shopping"
    await engine.dispose()


async def test_acat_callback_handles_missing_expense(tmp_path, monkeypatch):
    _, engine = await _sqlite_session_factory(tmp_path, monkeypatch)

    update, context, edits, _ = _make_callback(f"acat:{uuid4()}:food")
    await handle_audit_category_callback(update, context)

    assert edits == ["That expense no longer exists."]
    await engine.dispose()


async def test_acat_callback_ignores_malformed_data(tmp_path, monkeypatch):
    _, engine = await _sqlite_session_factory(tmp_path, monkeypatch)

    update, context, edits, _ = _make_callback("acat:not-enough-parts")
    await handle_audit_category_callback(update, context)

    assert edits == []  # bailed out without editing
    await engine.dispose()
