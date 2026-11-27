"""End-to-end smoke test against a REAL Postgres (run by CI after
`alembic upgrade head`). The normal test suite uses SQLite; this catches
what only Postgres enforces — column sizes, numeric precision, constraints —
using the real migrations, not Base.metadata.create_all.

    DATABASE_URL=postgresql+asyncpg://... python -m scripts.postgres_smoke

No LLM is called: the agent's replies are scripted. Exits non-zero on failure.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select, text

from app.agent_core.actions import resolve_pending_action
from app.agent_core.chat import answer_chat
from app.agent_core.reconcile import reconcile_with_agent
from app.audit import run_monthly_audit
from app.db import SessionLocal, engine
from app.ingestion import ingest_sms_transaction
from app.llm.base import AssistantTurn, ChatMessage, LLMDecisionError, ToolCall, ToolSpec
from app.models import AgentStep, AuditRun, Budget, Expense, Income, OwnAccount, User
from app.month_plan import get_month_plan


class ScriptedProvider:
    """Replays fixed agent replies; every other LLM method fails like a
    provider that is down (so the code's no-LLM fallbacks are exercised)."""

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = list(turns)

    async def chat_with_tools(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> AssistantTurn:
        if not self.turns:
            return AssistantTurn(text="done", tool_calls=[])
        return self.turns.pop(0)

    async def parse_transaction(self, raw_text: str):  # type: ignore[no-untyped-def]
        raise LLMDecisionError("no LLM in the smoke test")

    async def audit_finances(self, snapshot: dict):  # type: ignore[no-untyped-def]
        raise LLMDecisionError("no LLM in the smoke test")


def tool_call(name: str, args: dict) -> AssistantTurn:
    call = ToolCall(id="c1", name=name, args=args)
    return AssistantTurn(text="", tool_calls=[call], input_tokens=10, output_tokens=5)


def check(condition: bool, what: str) -> None:
    if not condition:
        raise SystemExit(f"FAILED: {what}")
    print(f"ok  {what}")


async def main() -> None:
    no_llm = ScriptedProvider([])
    async with SessionLocal() as session:
        await session.execute(text("TRUNCATE users CASCADE"))
        await session.commit()

        user = User(telegram_chat_id=99)
        session.add(user)
        await session.commit()
        session.add(OwnAccount(user_id=user.id, last4="4321", kind="savings"))
        await session.commit()

        # The varying-savings month (docs/AGENT_DESIGN.md §8).
        outcomes = []
        for sms in [
            "INR 50,000.00 credited to your A/c XX1234 on 01-11-26 by NEFT SALARY from EMPLOYER",
            "Rs.35,000.00 debited from A/c XX1234 on 01-11-26 and credited to A/c XX4321",
            "Rs.380.00 debited from A/c XX1234 on 11-11-26 to SWIGGY",
            "Rs.3,000.00 credited to A/c XX1234 on 20-11-26 from A/c XX4321",
        ]:
            result = await ingest_sms_transaction(session, user, sms, no_llm)
            outcomes.append(result.outcome)
        check(
            outcomes == ["income_recorded", "savings_transfer", "transaction_created", "savings_withdrawal"],
            f"SMS outcomes {outcomes}",
        )

        agent = ScriptedProvider(
            [tool_call("log_as_new_expense", {"category": "Food", "confidence": 0.9, "reason": "Swiggy is food"})]
        )
        summary = await reconcile_with_agent(session, agent, user.id)
        check(summary.auto_logged == 1, "agent logged the Swiggy transaction")

        plan = await get_month_plan(session, user.id, today=date(2026, 11, 25))
        check(plan.saved == Decimal("32000"), f"saved this month = 32,000 (got {plan.saved})")
        check(plan.spending_money == Decimal("18000"), f"spending money = 18,000 (got {plan.spending_money})")
        check(plan.money_left == Decimal("17620"), f"money left = 17,620 (got {plan.money_left})")

        # Chat change → Yes → applied once, even on a double tap.
        chat = ScriptedProvider([tool_call("set_budget", {"category": "Food", "monthly_limit": "4000"})])
        reply = await answer_chat(session, chat, user.id, "set my food budget to 4000")
        action_id = reply.confirmations[0].id
        user_id = user.id
        first, _ = await resolve_pending_action(session, action_id, user_id=user_id, approved=True)
        second, _ = await resolve_pending_action(session, action_id, user_id=user_id, approved=True)
        budgets = (await session.execute(select(func.count()).select_from(Budget))).scalar_one()
        check(first is not None and second is None and budgets == 1, "approved change applied exactly once")

        # Audit with a tiny income and a big spend: the savings rate must fit
        # Numeric(7,2) on Postgres (this used to crash).
        session.add(Income(user_id=user_id, amount=Decimal("1"), txn_date=date(2026, 10, 2), raw_text="c"))
        session.add(Expense(user_id=user_id, amount=Decimal("50000"), category="Food", txn_date=date(2026, 10, 3)))
        await session.commit()
        audit = await run_monthly_audit(session, no_llm, user_id, today=date(2026, 11, 5))
        stored = (await session.execute(select(AuditRun.savings_rate))).scalar_one()
        check(audit.status == "completed" and stored == Decimal("-99999.99"), "audit with a capped savings rate")

        steps = (await session.execute(select(func.count()).select_from(AgentStep))).scalar_one()
        check(steps > 0, f"agent steps traced ({steps})")

    await engine.dispose()
    print("Postgres smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
