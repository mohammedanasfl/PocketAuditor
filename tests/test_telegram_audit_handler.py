"""Phase 4: the /audit command handler. SimpleNamespace fakes + monkeypatched
SessionLocal, like the other handler tests, but the context also carries a fake
bot (the handler pushes via context.bot.send_message, same as /reconcile) and a
scripted llm_provider in bot_data.

Seeds the previous completed month relative to the real clock (the handler
doesn't take a `today` kwarg) so it doesn't depend on an exact sandbox date.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.telegram.handlers.commands as handlers_module
from app.db import Base
from app.models import Expense, Income, User
from app.schemas import AuditReport
from app.telegram.handlers import handle_audit_command


def _prev_month_day() -> date:
    month_start = date.today().replace(day=1)
    prev_end = month_start - timedelta(days=1)
    return prev_end.replace(day=1)


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup: object = None) -> None:
        self.messages.append((chat_id, text, reply_markup))


class _ScriptedProvider:
    def __init__(self, report: AuditReport) -> None:
        self._report = report

    async def audit_finances(self, snapshot: dict) -> AuditReport:
        return self._report


def _make_update_and_context(provider: _ScriptedProvider, bot: _FakeBot):
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=42))
    context = SimpleNamespace(args=[], bot=bot, bot_data={"llm_provider": provider})
    return update, context


async def _sqlite_session_factory(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(handlers_module, "SessionLocal", session_factory)
    return session_factory, engine


async def test_audit_command_sends_report_and_anomaly_question(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    day = _prev_month_day()

    async with session_factory() as session:
        user = User(telegram_chat_id=42)
        session.add(user)
        await session.commit()
        session.add(Income(user_id=user.id, amount=Decimal("50000"), source="ACME", txn_date=day, raw_text="x"))
        uncategorized = Expense(user_id=user.id, amount=Decimal("3000"), category="Uncategorized", txn_date=day)
        session.add(uncategorized)
        await session.commit()
        flagged_id = uncategorized.id

    provider = _ScriptedProvider(
        AuditReport(
            summary="Good month.", recommendations=["Save more."], flagged_expense_ids=[flagged_id], confidence=0.9
        )
    )
    bot = _FakeBot()
    update, context = _make_update_and_context(provider, bot)

    await handle_audit_command(update, context)

    # First message is the report; second is the anomaly question (with buttons).
    assert len(bot.messages) == 2
    assert "Salary audit" in bot.messages[0][1]
    assert bot.messages[0][2] is None
    assert bot.messages[1][2] is not None  # inline keyboard on the question
    await engine.dispose()


async def test_audit_command_reports_nothing_to_audit_when_empty(tmp_path, monkeypatch):
    _, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    provider = _ScriptedProvider(AuditReport(summary="", recommendations=[], flagged_expense_ids=[], confidence=0.5))
    bot = _FakeBot()
    update, context = _make_update_and_context(provider, bot)

    await handle_audit_command(update, context)

    assert len(bot.messages) == 1
    assert "Nothing to audit" in bot.messages[0][1]
    await engine.dispose()
