"""End-to-end provider parity: run the *full* agent loop twice — once against
OllamaProvider (respx-mocked HTTP), once against ClaudeProvider (monkeypatched
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

import respx
from httpx import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import reconcile_user
from app.db import Base
from app.llm.claude import ClaudeProvider
from app.llm.ollama import OllamaProvider
from app.models import Expense, ReconciliationRun, Transaction, User

OLLAMA_URL = "http://localhost:11434/api/chat"


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


@respx.mock
async def test_agent_loop_produces_identical_writes_across_providers(tmp_path, monkeypatch):
    decision_json = _decision_json()

    # --- Ollama run ---
    ollama_engine, ollama_session = await _fresh_session(tmp_path, "ollama")
    ollama_user = await _seed(ollama_session)
    respx.post(OLLAMA_URL).mock(return_value=Response(200, json={"message": {"content": decision_json}}))
    ollama_provider = OllamaProvider(base_url="http://localhost:11434", model="qwen2.5:7b")
    ollama_summary = await reconcile_user(ollama_session, ollama_provider, ollama_user.id)

    # --- Claude run ---
    claude_engine, claude_session = await _fresh_session(tmp_path, "claude")
    claude_user = await _seed(claude_session)

    async def fake_create(**kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=decision_json)])

    claude_provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(claude_provider._client.messages, "create", fake_create)
    claude_summary = await reconcile_user(claude_session, claude_provider, claude_user.id)

    assert ollama_summary.auto_logged == claude_summary.auto_logged == 1
    assert ollama_summary.as_message() == claude_summary.as_message()

    ollama_expense = (await ollama_session.execute(select(Expense))).scalar_one()
    claude_expense = (await claude_session.execute(select(Expense))).scalar_one()
    assert ollama_expense.amount == claude_expense.amount
    assert ollama_expense.category == claude_expense.category
    assert ollama_expense.created_via == claude_expense.created_via
    assert ollama_expense.merchant == claude_expense.merchant

    ollama_run = (await ollama_session.execute(select(ReconciliationRun))).scalar_one()
    claude_run = (await claude_session.execute(select(ReconciliationRun))).scalar_one()
    assert ollama_run.decision == claude_run.decision
    assert ollama_run.confidence == claude_run.confidence
    assert ollama_run.status == claude_run.status

    await ollama_session.close()
    await claude_session.close()
    await ollama_engine.dispose()
    await claude_engine.dispose()
