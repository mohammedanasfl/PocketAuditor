"""Stage 3b tests: the /ask command handler. Fixture questions are simulated
via a FakeProvider that returns pre-built QueryIntent objects (interpret_query
is never actually called against a real model here) — the point is to prove
the handler wires interpret_query -> run_query -> reply correctly, including
the two guardrails: no-data-found and interpret_query failure.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.telegram.handlers as handlers_module
import app.query as query_module
from app.db import Base
from app.llm.base import LLMDecisionError
from app.models import Expense, User
from app.schemas import QueryIntent
from app.telegram.handlers import handle_ask_command


def _intent(**overrides) -> QueryIntent:
    # date_range="custom" with explicit bounds spanning the seeded fixture
    # data — sidesteps any dependence on the real wall-clock date.today(),
    # which resolve_date_range's own dedicated tests (tests/test_query.py)
    # already cover for the named ranges.
    defaults = dict(
        is_expense_question=True,
        category="Food",
        date_range="custom",
        custom_start=date(2026, 8, 1),
        custom_end=date(2026, 8, 31),
        aggregation="sum",
        intent_summary="How much was spent on food this week.",
    )
    defaults.update(overrides)
    return QueryIntent(**defaults)


class _FakeProvider:
    def __init__(self, intent: QueryIntent | None = None, error: Exception | None = None):
        self._intent = intent
        self._error = error

    async def interpret_query(self, question: str) -> QueryIntent:
        if self._error is not None:
            raise self._error
        assert self._intent is not None
        return self._intent


def _make_update_and_context(question_words: list[str], provider) -> tuple:
    replies: list[str] = []

    async def reply_text(text: str) -> None:
        replies.append(text)

    message = SimpleNamespace(reply_text=reply_text)
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=777))
    context = SimpleNamespace(args=question_words, bot_data={"llm_provider": provider})
    return update, context, replies


async def _sqlite_session_factory(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ask.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(handlers_module, "SessionLocal", session_factory)
    return session_factory, engine


async def _seed_user_and_expenses(session_factory) -> None:
    async with session_factory() as session:
        user = User(telegram_chat_id=777)
        session.add(user)
        await session.commit()
        session.add_all(
            [
                Expense(user_id=user.id, amount=Decimal("200.00"), category="Food", txn_date=date(2026, 8, 11)),
                Expense(user_id=user.id, amount=Decimal("300.00"), category="Food", txn_date=date(2026, 8, 12)),
                Expense(user_id=user.id, amount=Decimal("999.00"), category="Transport", txn_date=date(2026, 8, 12)),
            ]
        )
        await session.commit()


async def test_ask_sum_question_returns_correct_numeric_answer(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    await _seed_user_and_expenses(session_factory)

    provider = _FakeProvider(intent=_intent(category="Food", aggregation="sum"))
    update, context, replies = _make_update_and_context(["how", "much", "on", "food", "this", "week"], provider)

    await handle_ask_command(update, context)

    assert len(replies) == 1
    assert "500.00" in replies[0]
    assert "2 transactions" in replies[0]
    await engine.dispose()


async def test_ask_biggest_expense_question(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    await _seed_user_and_expenses(session_factory)

    provider = _FakeProvider(intent=_intent(category=None, aggregation="max"))
    update, context, replies = _make_update_and_context(["biggest", "expense", "this", "week"], provider)

    await handle_ask_command(update, context)

    assert len(replies) == 1
    assert "999.00" in replies[0]
    await engine.dispose()


async def test_ask_with_no_matching_data_replies_plainly(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    await _seed_user_and_expenses(session_factory)

    provider = _FakeProvider(intent=_intent(category="Entertainment", aggregation="sum"))
    update, context, replies = _make_update_and_context(["fuel", "payments"], provider)

    await handle_ask_command(update, context)

    assert replies == ["No expenses found for that period."]
    await engine.dispose()


async def test_ask_nonsense_question_gets_graceful_fallback(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    await _seed_user_and_expenses(session_factory)

    provider = _FakeProvider(error=LLMDecisionError("could not parse"))
    update, context, replies = _make_update_and_context(["asdkfjaslkdfj"], provider)

    await handle_ask_command(update, context)

    assert len(replies) == 1
    assert "couldn't understand" in replies[0]
    await engine.dispose()


async def test_ask_question_unrelated_to_expenses_does_not_run_a_query(tmp_path, monkeypatch):
    """Regression: "what is API?" must not come back with a real (but
    irrelevant) spend figure just because date_range/aggregation still had
    to be *something* — is_expense_question=false must be trusted and
    short-circuit before run_query is ever called."""
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    await _seed_user_and_expenses(session_factory)

    received_calls = []
    real_run_query = query_module.run_query

    async def _spy_run_query(*args, **kwargs):
        received_calls.append((args, kwargs))
        return await real_run_query(*args, **kwargs)

    monkeypatch.setattr(handlers_module, "run_query", _spy_run_query)

    provider = _FakeProvider(intent=_intent(is_expense_question=False, intent_summary="What is API?"))
    update, context, replies = _make_update_and_context(["what", "is", "api", "?"], provider)

    await handle_ask_command(update, context)

    assert received_calls == []  # run_query never called
    assert len(replies) == 1
    assert "only answer questions about your spending" in replies[0]
    await engine.dispose()


async def test_ask_with_no_question_shows_usage(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    provider = _FakeProvider()
    update, context, replies = _make_update_and_context([], provider)

    await handle_ask_command(update, context)

    assert len(replies) == 1 and "Usage" in replies[0]
    await engine.dispose()


async def test_ask_handler_never_passes_raw_question_text_to_run_query(tmp_path, monkeypatch):
    """The actual safety property from the brief: run_query only ever
    receives a validated QueryIntent, never the free-text question."""
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    await _seed_user_and_expenses(session_factory)

    received_args = []

    async def _spy_run_query(session, user_id, intent, **kwargs):
        received_args.append(intent)
        return query_module.QueryResult(intent=intent, start=date(2026, 8, 10), end=date(2026, 8, 15))

    monkeypatch.setattr(handlers_module, "run_query", _spy_run_query)

    intent = _intent(category="Food", aggregation="sum")
    provider = _FakeProvider(intent=intent)
    update, context, replies = _make_update_and_context(
        ["how", "much", "on", "food", "this", "week", "'; DROP", "TABLE", "expenses;"], provider
    )

    await handle_ask_command(update, context)

    assert len(received_args) == 1
    assert isinstance(received_args[0], QueryIntent)
    assert received_args[0] is intent  # exactly the validated object, nothing re-derived from the raw text
    await engine.dispose()
