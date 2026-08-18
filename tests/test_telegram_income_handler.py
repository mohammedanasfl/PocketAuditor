"""Phase 4: the /income command handler. Same SimpleNamespace + monkeypatched
SessionLocal pattern as tests/test_telegram_budget_handlers.py — no live
Telegram, and the handler's own SessionLocal is pointed at a fresh sqlite DB.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.telegram.handlers.commands as handlers_module
from app.db import Base
from app.models import Income, User
from app.telegram.handlers import handle_income_command


def _make_update_and_context():
    replies: list[str] = []

    async def reply_text(text: str) -> None:
        replies.append(text)

    message = SimpleNamespace(reply_text=reply_text)
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=42))
    context = SimpleNamespace(args=[])
    return update, context, replies


async def _sqlite_session_factory(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'income.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(handlers_module, "SessionLocal", session_factory)
    return session_factory, engine


async def test_income_command_reports_zero_for_new_user(tmp_path, monkeypatch):
    _, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context()

    await handle_income_command(update, context)

    assert len(replies) == 1
    assert "💵 Income summary" in replies[0]
    assert "This month: Rs.0.00" in replies[0]
    await engine.dispose()


async def test_income_command_sums_this_months_income(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)

    async with session_factory() as session:
        user = User(telegram_chat_id=42)
        session.add(user)
        await session.commit()
        session.add(
            Income(user_id=user.id, amount=Decimal("50000.00"), source="ACME", txn_date=date.today(), raw_text="x")
        )
        await session.commit()

    update, context, replies = _make_update_and_context()
    await handle_income_command(update, context)

    assert len(replies) == 1
    assert "This month: Rs.50,000.00" in replies[0]
    await engine.dispose()
