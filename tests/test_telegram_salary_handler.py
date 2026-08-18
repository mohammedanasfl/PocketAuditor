"""Phase 4: the /salary command handler. SimpleNamespace + monkeypatched
SessionLocal, same as tests/test_telegram_budget_handlers.py."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.telegram.handlers.commands as handlers_module
from app.db import Base
from app.models import SalaryProfile
from app.telegram.handlers import handle_salary_command


def _make_update_and_context(args: list[str]):
    replies: list[str] = []

    async def reply_text(text: str) -> None:
        replies.append(text)

    message = SimpleNamespace(reply_text=reply_text)
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=42))
    context = SimpleNamespace(args=args)
    return update, context, replies


async def _sqlite_session_factory(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'salary.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(handlers_module, "SessionLocal", session_factory)
    return session_factory, engine


async def test_salary_sets_all_fields(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["50000", "10000", "1"])

    await handle_salary_command(update, context)

    async with session_factory() as session:
        profile = (await session.execute(select(SalaryProfile))).scalar_one()
    assert profile.expected_salary == Decimal("50000")
    assert profile.savings_target == Decimal("10000")
    assert profile.payday_day == 1
    assert len(replies) == 1 and "saved" in replies[0].lower()
    await engine.dispose()


async def test_salary_expected_only(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["50000"])

    await handle_salary_command(update, context)

    async with session_factory() as session:
        profile = (await session.execute(select(SalaryProfile))).scalar_one()
    assert profile.expected_salary == Decimal("50000")
    assert profile.savings_target is None
    assert profile.payday_day is None
    await engine.dispose()


async def test_salary_no_args_shows_usage_when_unset(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context([])

    await handle_salary_command(update, context)

    async with session_factory() as session:
        assert (await session.execute(select(SalaryProfile))).scalars().all() == []
    assert len(replies) == 1 and "Usage" in replies[0]
    await engine.dispose()


async def test_salary_no_args_shows_current_profile_when_set(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    set_update, set_context, _ = _make_update_and_context(["50000", "10000"])
    await handle_salary_command(set_update, set_context)

    update, context, replies = _make_update_and_context([])
    await handle_salary_command(update, context)

    assert len(replies) == 1
    assert "Rs.50,000.00" in replies[0] and "Usage" not in replies[0]
    await engine.dispose()


async def test_salary_rejects_non_positive_expected(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["0"])

    await handle_salary_command(update, context)

    async with session_factory() as session:
        assert (await session.execute(select(SalaryProfile))).scalars().all() == []
    assert len(replies) == 1 and "positive" in replies[0]
    await engine.dispose()


async def test_salary_rejects_savings_target_above_expected(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["50000", "60000"])

    await handle_salary_command(update, context)

    async with session_factory() as session:
        assert (await session.execute(select(SalaryProfile))).scalars().all() == []
    assert len(replies) == 1 and "Savings target" in replies[0]
    await engine.dispose()


async def test_salary_rejects_out_of_range_payday(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["50000", "10000", "31"])

    await handle_salary_command(update, context)

    async with session_factory() as session:
        assert (await session.execute(select(SalaryProfile))).scalars().all() == []
    assert len(replies) == 1 and "Payday" in replies[0]
    await engine.dispose()


async def test_salary_rejects_malformed_amount(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, context, replies = _make_update_and_context(["lots"])

    await handle_salary_command(update, context)

    async with session_factory() as session:
        assert (await session.execute(select(SalaryProfile))).scalars().all() == []
    assert len(replies) == 1 and "Usage" in replies[0]
    await engine.dispose()
