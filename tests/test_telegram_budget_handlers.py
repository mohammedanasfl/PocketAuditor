"""Stage 3a tests: the /setbudget and /budgets command handlers."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.telegram.handlers as handlers_module
from app.db import Base
from app.models import Budget
from app.telegram.handlers import handle_budgets_command, handle_setbudget_command


def _make_update_and_context(args: list[str]):
    replies: list[str] = []

    async def reply_text(text: str) -> None:
        replies.append(text)

    message = SimpleNamespace(reply_text=reply_text)
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=42))
    context = SimpleNamespace(args=args)
    return update, context, replies


async def _sqlite_session_factory(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'budgets.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(handlers_module, "SessionLocal", session_factory)
    return session_factory, engine


async def test_setbudget_creates_a_budget_row(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["Food", "4000"])

    await handle_setbudget_command(update, context)

    async with session_factory() as session:
        budgets = (await session.execute(select(Budget))).scalars().all()
    assert len(budgets) == 1
    assert budgets[0].category == "Food"
    assert budgets[0].monthly_limit == Decimal("4000")
    assert len(replies) == 1 and "Food" in replies[0]
    await engine.dispose()


async def test_setbudget_is_case_insensitive_against_known_categories(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["food", "4000"])

    await handle_setbudget_command(update, context)

    async with session_factory() as session:
        budget = (await session.execute(select(Budget))).scalar_one()
    assert budget.category == "Food"  # normalized to the canonical casing
    await engine.dispose()


async def test_setbudget_rejects_unknown_category(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["Crypto", "4000"])

    await handle_setbudget_command(update, context)

    async with session_factory() as session:
        assert (await session.execute(select(Budget))).scalars().all() == []
    assert len(replies) == 1 and "Unknown category" in replies[0]
    await engine.dispose()


async def test_setbudget_rejects_non_positive_amount(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["Food", "-50"])

    await handle_setbudget_command(update, context)

    async with session_factory() as session:
        assert (await session.execute(select(Budget))).scalars().all() == []
    assert len(replies) == 1
    await engine.dispose()


async def test_setbudget_rejects_malformed_amount(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["Food", "lots"])

    await handle_setbudget_command(update, context)

    async with session_factory() as session:
        assert (await session.execute(select(Budget))).scalars().all() == []
    assert len(replies) == 1
    await engine.dispose()


async def test_setbudget_shows_usage_when_missing_args(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["Food"])

    await handle_setbudget_command(update, context)

    assert len(replies) == 1 and "Usage" in replies[0]
    await engine.dispose()


async def test_setbudget_upserts_on_repeated_call(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update1, context1, _ = _make_update_and_context(["Food", "4000"])
    await handle_setbudget_command(update1, context1)

    update2, context2, _ = _make_update_and_context(["Food", "5000"])
    await handle_setbudget_command(update2, context2)

    async with session_factory() as session:
        budgets = (await session.execute(select(Budget))).scalars().all()
    assert len(budgets) == 1
    assert budgets[0].monthly_limit == Decimal("5000")
    await engine.dispose()


async def test_budgets_command_reports_no_budgets_set(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context([])

    await handle_budgets_command(update, context)

    assert len(replies) == 1 and "No budgets set" in replies[0]
    await engine.dispose()


async def test_budgets_command_lists_set_budgets(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    setbudget_update, setbudget_context, _ = _make_update_and_context(["Food", "4000"])
    await handle_setbudget_command(setbudget_update, setbudget_context)

    update, context, replies = _make_update_and_context([])
    await handle_budgets_command(update, context)

    assert len(replies) == 1
    assert "Food" in replies[0] and "4,000" in replies[0]
    await engine.dispose()
