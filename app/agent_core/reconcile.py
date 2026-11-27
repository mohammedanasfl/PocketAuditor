"""Process a user's pending transactions with the agent.

For each pending transaction:
1. Remembered merchant, nothing to match, no double charge → log it
   directly. Plain code, no LLM call (same shortcut as the pipeline).
2. Otherwise the agent decides, using the reconcile tools; the policy layer
   checks every write.
3. If the agent ends without a decision, ask the user — never leave a
   transaction silently unhandled, never guess.

reconcile() is what /reconcile, the cron job and new SMS call. It picks the
engine from settings.reconcile_engine ("agent" or "pipeline").
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import (
    RunSummary,
    act_on_decision,
    find_candidates,
    find_recent_duplicate_expense,
    lock_for,
    reconcile_user,
    transaction_to_dict,
)
from app.agent_core.loop import run_agent
from app.agent_core.prompts import RECONCILE_SYSTEM
from app.agent_core.tools import RECONCILE_TOOLS, AskUserArgs, ToolContext, ask_user_for_category
from app.config import settings
from app.llm.base import LLMProvider
from app.merchant_memory import recall_merchant_category
from app.models import ReconciliationRun, Transaction
from app.schemas import MatchDecision

logger = logging.getLogger(__name__)


async def reconcile(session: AsyncSession, provider: LLMProvider, user_id: UUID) -> RunSummary:
    """Process every pending transaction with the configured engine."""
    if settings.reconcile_engine == "pipeline":
        return await reconcile_user(session, provider, user_id)
    return await reconcile_with_agent(session, provider, user_id)


async def reconcile_with_agent(session: AsyncSession, provider: LLMProvider, user_id: UUID) -> RunSummary:
    async with lock_for(user_id):  # one reconcile per user at a time
        summary = RunSummary()
        for txn in await _transactions_to_process(session, user_id):
            outcome = await _process_one(session, provider, user_id, txn, summary)
            if outcome == "auto_link":
                summary.auto_linked += 1
            elif outcome == "auto_log":
                summary.auto_logged += 1
            elif outcome == "ask_user":
                summary.asked_user += 1
            else:
                summary.errors += 1
        logger.info("reconcile_with_agent: user=%s — %s", user_id, summary.as_message())
        return summary


async def _transactions_to_process(session: AsyncSession, user_id: UUID) -> list[Transaction]:
    """Pending transactions, oldest first, minus ones already waiting on the user."""
    pending = (
        (
            await session.execute(
                select(Transaction)
                .where(Transaction.user_id == user_id, Transaction.status == "pending")
                .order_by(Transaction.created_at)
            )
        )
        .scalars()
        .all()
    )
    already_asked = set(
        (
            await session.execute(
                select(ReconciliationRun.transaction_id).where(
                    ReconciliationRun.user_id == user_id, ReconciliationRun.status == "open"
                )
            )
        )
        .scalars()
        .all()
    )
    return [txn for txn in pending if txn.id not in already_asked]


async def _process_one(
    session: AsyncSession, provider: LLMProvider, user_id: UUID, txn: Transaction, summary: RunSummary
) -> str | None:
    """Handle one transaction. Returns the outcome, or None if it failed
    (the transaction then stays pending and is retried next time)."""
    # 1. Remembered merchant — no LLM needed.
    if await _log_from_memory(session, user_id, txn, summary):
        return "auto_log"

    # 2. Let the agent decide.
    ctx = ToolContext(session=session, user_id=user_id, transaction=txn)
    goal = "Process this transaction:\n" + json.dumps(transaction_to_dict(txn), indent=2)
    result = await run_agent(
        session=session,
        provider=provider,
        ctx=ctx,
        trigger="sms",
        system=RECONCILE_SYSTEM,
        goal=goal,
        tools=RECONCILE_TOOLS,
    )

    # 3. No decision. If the model itself failed, leave it pending for the
    # next run; otherwise ask the user rather than leave it hanging.
    if ctx.outcome is None and result.status not in ("failed", "over_daily_limit"):
        await ask_user_for_category(ctx, AskUserArgs(reason="I couldn't decide this one on my own."))

    summary.pending_questions.extend(ctx.pending_questions)
    return ctx.outcome  # None when the model failed


async def _log_from_memory(session: AsyncSession, user_id: UUID, txn: Transaction, summary: RunSummary) -> bool:
    """The deterministic shortcut: if the user already told us this merchant's
    category, there is nothing to match, and it's not a double charge — log it."""
    if not txn.merchant or txn.category_hint:
        return False
    remembered = await recall_merchant_category(session, user_id, txn.merchant)
    if remembered is None:
        return False
    if await find_candidates(session, user_id, txn):
        return False  # a hand-logged expense might match — let the agent look
    if await find_recent_duplicate_expense(session, user_id, txn):
        return False  # possible double charge — let the agent/policy ask
    decision = MatchDecision(
        action="auto_log",
        matched_expense_id=None,
        suggested_category=remembered,
        confidence=1.0,
        reasoning=f"Remembered category for merchant '{txn.merchant}' from a previous manual choice.",
    )
    await act_on_decision(session, user_id, txn, decision)
    return True
