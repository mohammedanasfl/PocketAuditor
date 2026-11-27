"""End-to-end provider parity for the monthly audit: run run_monthly_audit
twice — once against GeminiProvider (monkeypatched genai client), once against ClaudeProvider
(monkeypatched) — feeding both the same model JSON, and assert the resulting
audit_runs rows and report messages match. The Phase 4 analog of
tests/test_agent_provider_parity.py.

Uses an empty flagged_expense_ids so the identical model JSON stays valid
across both runs (a flagged id would reference a different expense id in each
fresh DB); the flagged-id path is covered in tests/test_audit.py.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit import run_monthly_audit
from app.db import Base
from app.llm.claude import ClaudeProvider
from app.llm.gemini import GeminiProvider
from app.models import AuditRun, Expense, Income, User

TODAY = date(2026, 8, 15)


async def _fresh_session(tmp_path, name: str):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)()


async def _seed(session) -> User:
    user = User(telegram_chat_id=999)
    session.add(user)
    await session.commit()
    session.add(
        Income(user_id=user.id, amount=Decimal("50000"), source="ACME", txn_date=date(2026, 7, 1), raw_text="x")
    )
    session.add(Expense(user_id=user.id, amount=Decimal("38000"), category="Food", txn_date=date(2026, 7, 10)))
    await session.commit()
    return user


def _report_json() -> str:
    return json.dumps(
        {
            "summary": "You saved a good share of your income this month.",
            "recommendations": ["Keep the momentum."],
            "flagged_expense_ids": [],
            "confidence": 0.9,
        }
    )


async def test_audit_produces_identical_writes_across_providers(tmp_path, monkeypatch):
    report_json = _report_json()

    gemini_engine, gemini_session = await _fresh_session(tmp_path, "gemini")
    gemini_user = await _seed(gemini_session)

    async def fake_generate(**kwargs):
        return SimpleNamespace(text=report_json)

    gemini_provider = GeminiProvider(api_key="test-key", model="gemini-3.5-flash-lite")
    monkeypatch.setattr(gemini_provider._client.aio.models, "generate_content", fake_generate)
    gemini_result = await run_monthly_audit(gemini_session, gemini_provider, gemini_user.id, today=TODAY)

    claude_engine, claude_session = await _fresh_session(tmp_path, "claude")
    claude_user = await _seed(claude_session)

    async def fake_create(**kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=report_json)])

    claude_provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(claude_provider._client.messages, "create", fake_create)
    claude_result = await run_monthly_audit(claude_session, claude_provider, claude_user.id, today=TODAY)

    assert gemini_result.status == claude_result.status == "completed"
    assert gemini_result.message == claude_result.message

    gemini_run = (await gemini_session.execute(select(AuditRun))).scalar_one()
    claude_run = (await claude_session.execute(select(AuditRun))).scalar_one()
    assert gemini_run.total_income == claude_run.total_income == Decimal("50000")
    assert gemini_run.total_spend == claude_run.total_spend == Decimal("38000")
    assert gemini_run.net_saved == claude_run.net_saved == Decimal("12000")
    assert gemini_run.savings_rate == claude_run.savings_rate
    assert gemini_run.summary == claude_run.summary

    await gemini_session.close()
    await claude_session.close()
    await gemini_engine.dispose()
    await claude_engine.dispose()
