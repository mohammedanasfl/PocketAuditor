"""End-to-end provider parity: run the *full* agent loop twice — once against
GeminiProvider (monkeypatched genai client), once against ClaudeProvider (monkeypatched
Anthropic client) — feeding both the same underlying model JSON, and assert
the resulting expenses + reconciliation_runs rows match. This is the concrete
test the brief calls for ("assert the decision loop behaves identically
regardless of provider"), exercised through the real loop rather than just
decide_match in isolation (see tests/test_llm_providers.py for that).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import reconcile_user
from app.db import Base
from app.llm.claude import ClaudeProvider
from app.llm.gemini import GeminiProvider
from app.models import Expense, ReconciliationRun, Transaction, User


async def _fresh_session(tmp_path, name: str):
    db_path = tmp_path / f"{name}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    return engine, session_factory()


async def _seed(session) -> User:
    user = User(telegram_chat_id=999)
    session.add(user)
    await session.commit()

    txn = Transaction(
        user_id=user.id,
        raw_text="Rs.450.00 debited towards Blinkit",
        amount=Decimal("450.00"),
        merchant="Blinkit",
        txn_date=date(2026, 8, 10),
        status="pending",
    )
    session.add(txn)
    await session.commit()
    return user


def _decision_json() -> str:
    return json.dumps(
        {
            "action": "auto_log",
            "matched_expense_id": None,
            "suggested_category": "Food",
            "confidence": 0.9,
            "reasoning": "Clear grocery merchant and amount with no matching candidate.",
        }
    )


async def test_agent_loop_produces_identical_writes_across_providers(tmp_path, monkeypatch):
    decision_json = _decision_json()

    # --- Gemini run ---
    gemini_engine, gemini_session = await _fresh_session(tmp_path, "gemini")
    gemini_user = await _seed(gemini_session)

    async def fake_generate(**kwargs):
        return SimpleNamespace(text=decision_json)

    gemini_provider = GeminiProvider(api_key="test-key", model="gemini-3.5-flash-lite")
    monkeypatch.setattr(gemini_provider._client.aio.models, "generate_content", fake_generate)
    gemini_summary = await reconcile_user(gemini_session, gemini_provider, gemini_user.id)

    # --- Claude run ---
    claude_engine, claude_session = await _fresh_session(tmp_path, "claude")
    claude_user = await _seed(claude_session)

    async def fake_create(**kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=decision_json)])

    claude_provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(claude_provider._client.messages, "create", fake_create)
    claude_summary = await reconcile_user(claude_session, claude_provider, claude_user.id)

    assert gemini_summary.auto_logged == claude_summary.auto_logged == 1
    assert gemini_summary.as_message() == claude_summary.as_message()

    gemini_expense = (await gemini_session.execute(select(Expense))).scalar_one()
    claude_expense = (await claude_session.execute(select(Expense))).scalar_one()
    assert gemini_expense.amount == claude_expense.amount
    assert gemini_expense.category == claude_expense.category
    assert gemini_expense.created_via == claude_expense.created_via
    assert gemini_expense.merchant == claude_expense.merchant

    gemini_run = (await gemini_session.execute(select(ReconciliationRun))).scalar_one()
    claude_run = (await claude_session.execute(select(ReconciliationRun))).scalar_one()
    assert gemini_run.decision == claude_run.decision
    assert gemini_run.confidence == claude_run.confidence
    assert gemini_run.status == claude_run.status

    await gemini_session.close()
    await claude_session.close()
    await gemini_engine.dispose()
    await claude_engine.dispose()
