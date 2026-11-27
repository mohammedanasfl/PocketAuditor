"""Run a change the user approved (a PendingAction) — plain code only.

The chat agent proposed the change; the policy turned it into a Yes/No
question; the user tapped Yes. Now the stored arguments are applied by the
normal service functions. The model is not involved at this point.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.budgets import upsert_budget
from app.categories import normalize_category
from app.db import insert_once
from app.expenses import log_manual_expense, recategorize_expense
from app.models import OwnAccount, PendingAction

logger = logging.getLogger(__name__)


async def resolve_pending_action(
    session: AsyncSession, action_id: UUID, *, user_id: UUID, approved: bool
) -> tuple[PendingAction | None, str]:
    """Apply (or drop) a waiting action. Returns (action, message for the user).
    The action is None if it doesn't exist, isn't this user's, or was
    already answered (e.g. a double tap)."""
    # Claim the action in ONE statement: only the first of two quick taps
    # changes 'waiting' to an answer, so a double tap can't apply it twice.
    claim = await session.execute(
        update(PendingAction)
        .where(PendingAction.id == action_id, PendingAction.user_id == user_id, PendingAction.status == "waiting")
        .values(status="approved" if approved else "rejected", resolved_at=datetime.now(UTC))
    )
    await session.commit()
    if claim.rowcount != 1:  # type: ignore[attr-defined]
        return None, "This was already answered."

    action = await session.get(PendingAction, action_id)
    assert action is not None
    await session.refresh(action)
    if not approved:
        return action, "OK, I didn't change anything."
    message = await _apply(session, action)
    logger.info("pending action=%s (%s) applied for user=%s", action.id, action.tool_name, user_id)
    return action, message


async def _apply(session: AsyncSession, action: PendingAction) -> str:
    args = json.loads(action.args)
    user_id = action.user_id

    if action.tool_name == "set_budget":
        category = normalize_category(args["category"])
        assert category is not None  # checked by the policy before it was stored
        limit = Decimal(args["monthly_limit"])
        await upsert_budget(session, user_id, category, limit)
        return f"✅ {category} budget set to Rs.{limit:,.2f} a month."

    if action.tool_name == "recategorize_expense":
        category = normalize_category(args["category"])
        assert category is not None
        expense = await recategorize_expense(session, UUID(args["expense_id"]), category, user_id=user_id)
        if expense is None:
            return "That expense no longer exists."
        return f"✅ Moved Rs.{expense.amount:,.2f} to {category}."

    if action.tool_name == "log_cash_expense":
        category = normalize_category(args["category"])
        assert category is not None
        amount = Decimal(args["amount"])
        await log_manual_expense(session, user_id, category, amount, args.get("note"))
        return f"✅ Logged Rs.{amount:,.2f} under {category}."

    if action.tool_name == "remember_savings_account":
        await insert_once(session, OwnAccount(user_id=user_id, last4=args["last4"], kind="savings"))
        return f"✅ I'll treat account ending {args['last4']} as your savings account."

    return "I don't know how to do that."
