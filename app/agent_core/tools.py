"""The tools the agent can call.

Each tool is:
- an arguments model (Pydantic) — its JSON Schema is what the model sees,
- a plain async function `run(ctx, args) -> dict` — what actually happens,
- a kind: "read" tools run freely, "write" tools are checked by
  app/agent_core/policy.py first.

The reconcile tools always act on ONE transaction — the one this run is
about (ctx.transaction). The model never passes a transaction id, so it
cannot point a write at the wrong transaction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import (
    PendingQuestion,
    act_on_decision,
    expense_to_dict,
    find_candidates,
    find_recent_duplicate_expense,
)
from app.categories import CATEGORIES, normalize_category
from app.llm.base import ToolSpec
from app.merchant_memory import recall_merchant_category
from app.models import Transaction
from app.schemas import MatchDecision


@dataclass
class ToolContext:
    """Everything a tool needs, plus what happened during the run."""

    session: AsyncSession
    user_id: UUID
    # The transaction a reconcile run is about (None for chat runs).
    transaction: Transaction | None = None
    # Expense ids the model has actually been shown. The policy denies a
    # link to any other id — that would be a made-up (hallucinated) id.
    seen_expense_ids: set[str] = field(default_factory=set)
    # What was done with ctx.transaction: "auto_link" | "auto_log" | "ask_user".
    outcome: str | None = None
    # Questions to send to the user once the run is over.
    pending_questions: list[PendingQuestion] = field(default_factory=list)
    # Messages a chat run wants sent with buttons (filled by chat tools).
    confirmations: list[Any] = field(default_factory=list)


@dataclass
class Tool:
    name: str
    description: str
    kind: Literal["read", "write"]
    args_model: type[BaseModel]
    run: Callable[[ToolContext, Any], Awaitable[dict]]
    # For write tools: what to do when the policy says "ask the user first".
    ask_instead: Callable[[ToolContext, Any, str], Awaitable[dict]] | None = None

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.args_model.model_json_schema())


class NoArgs(BaseModel):
    pass


CATEGORY_NAMES = list(CATEGORIES.values())


# --- reconcile: read tools ------------------------------------------------


async def find_matching_expenses(ctx: ToolContext, args: NoArgs) -> dict:
    assert ctx.transaction is not None
    candidates = await find_candidates(ctx.session, ctx.user_id, ctx.transaction)
    for expense in candidates:
        ctx.seen_expense_ids.add(str(expense.id))
    return {"candidates": [expense_to_dict(expense) for expense in candidates]}


async def recall_merchant(ctx: ToolContext, args: NoArgs) -> dict:
    assert ctx.transaction is not None
    category = await recall_merchant_category(ctx.session, ctx.user_id, ctx.transaction.merchant)
    return {"remembered_category": category}


async def check_duplicate_charge(ctx: ToolContext, args: NoArgs) -> dict:
    assert ctx.transaction is not None
    if not ctx.transaction.merchant:
        return {"possible_duplicate": None}
    duplicate = await find_recent_duplicate_expense(ctx.session, ctx.user_id, ctx.transaction)
    return {"possible_duplicate": expense_to_dict(duplicate) if duplicate else None}


# --- reconcile: write tools -----------------------------------------------


class LogExpenseArgs(BaseModel):
    category: str = Field(description=f"Exactly one of: {', '.join(CATEGORY_NAMES)}")
    confidence: float = Field(ge=0.0, le=1.0, description="How sure you are, 0 to 1")
    reason: str = Field(description="One sentence: why this category")


class LinkExpenseArgs(BaseModel):
    expense_id: str = Field(description="id of a candidate returned by find_matching_expenses")
    confidence: float = Field(ge=0.0, le=1.0, description="How sure you are, 0 to 1")
    reason: str = Field(description="One sentence: why this is the same spend")


class AskUserArgs(BaseModel):
    suggested_category: str | None = Field(default=None, description="Your best guess, or null")
    reason: str = Field(description="One sentence: what is unclear")


async def _act(ctx: ToolContext, decision: MatchDecision) -> dict:
    """Write the decision through the same code the pipeline uses, so both
    engines leave the exact same rows behind (expense + reconciliation_runs)."""
    assert ctx.transaction is not None
    question = await act_on_decision(ctx.session, ctx.user_id, ctx.transaction, decision)
    ctx.outcome = decision.action
    if question is not None:
        ctx.pending_questions.append(question)
    return {"status": decision.action}


async def log_as_new_expense(ctx: ToolContext, args: LogExpenseArgs) -> dict:
    category = normalize_category(args.category)
    assert category is not None  # the policy only allows known categories
    decision = MatchDecision(
        action="auto_log",
        matched_expense_id=None,
        suggested_category=category,
        confidence=args.confidence,
        reasoning=args.reason,
    )
    return await _act(ctx, decision)


async def link_to_expense(ctx: ToolContext, args: LinkExpenseArgs) -> dict:
    decision = MatchDecision(
        action="auto_link",
        matched_expense_id=UUID(args.expense_id),
        suggested_category=None,
        confidence=args.confidence,
        reasoning=args.reason,
    )
    return await _act(ctx, decision)


async def ask_user_for_category(ctx: ToolContext, args: AskUserArgs) -> dict:
    decision = MatchDecision(
        action="ask_user",
        matched_expense_id=None,
        suggested_category=normalize_category(args.suggested_category or ""),
        confidence=0.0,
        reasoning=args.reason,
    )
    return await _act(ctx, decision)


async def _ask_instead_of_logging(ctx: ToolContext, args: LogExpenseArgs, policy_reason: str) -> dict:
    ask_args = AskUserArgs(suggested_category=args.category, reason=f"{args.reason} [asking: {policy_reason}]")
    await ask_user_for_category(ctx, ask_args)
    return {"status": "asked_user_instead", "why": policy_reason}


async def _ask_instead_of_linking(ctx: ToolContext, args: LinkExpenseArgs, policy_reason: str) -> dict:
    ask_args = AskUserArgs(suggested_category=None, reason=f"{args.reason} [asking: {policy_reason}]")
    await ask_user_for_category(ctx, ask_args)
    return {"status": "asked_user_instead", "why": policy_reason}


RECONCILE_TOOLS = [
    Tool(
        name="find_matching_expenses",
        description="Expenses the user already logged by hand with a similar amount and date (up to 3).",
        kind="read",
        args_model=NoArgs,
        run=find_matching_expenses,
    ),
    Tool(
        name="recall_merchant",
        description="The category the user chose for this merchant before, if any.",
        kind="read",
        args_model=NoArgs,
        run=recall_merchant,
    ),
    Tool(
        name="check_duplicate_charge",
        description="An existing expense with the same merchant and amount in the last few days, if any.",
        kind="read",
        args_model=NoArgs,
        run=check_duplicate_charge,
    ),
    Tool(
        name="log_as_new_expense",
        description="Save this transaction as a new expense in the given category.",
        kind="write",
        args_model=LogExpenseArgs,
        run=log_as_new_expense,
        ask_instead=_ask_instead_of_logging,
    ),
    Tool(
        name="link_to_expense",
        description="Mark this transaction as the same spend as an expense the user already logged.",
        kind="write",
        args_model=LinkExpenseArgs,
        run=link_to_expense,
        ask_instead=_ask_instead_of_linking,
    ),
    Tool(
        name="ask_user_for_category",
        description="Ask the user to pick the category with buttons. Always safe when unsure.",
        kind="write",
        args_model=AskUserArgs,
        run=ask_user_for_category,
    ),
]
