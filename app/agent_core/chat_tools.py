"""Tools for the chat agent (free-form questions and requests).

Read tools return numbers computed by plain code — the model only repeats
them. Write tools never change anything directly: the policy turns every
one into a Yes/No question (a PendingAction), and the change only happens
when the user taps Yes (app/agent_core/actions.py).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.agent_core.tools import CATEGORY_NAMES, NoArgs, Tool, ToolContext
from app.budgets import get_budget_statuses
from app.categories import normalize_category
from app.models import Expense, PendingAction
from app.month_plan import get_month_plan
from app.query import run_query
from app.salary import get_salary_profile
from app.schemas import QueryIntent

DateRange = Literal["today", "this_week", "last_week", "this_month", "last_month", "custom"]


class PeriodArgs(BaseModel):
    date_range: DateRange = Field(description="Which period. Use custom with start/end for anything else.")
    custom_start: date | None = Field(default=None, description="First day, only for custom")
    custom_end: date | None = Field(default=None, description="Last day, only for custom")


class SpendingArgs(PeriodArgs):
    category: str | None = Field(default=None, description=f"One of: {', '.join(CATEGORY_NAMES)} — or null for all")


def _intent(args: PeriodArgs, aggregation: Literal["sum", "net"], category: str | None) -> QueryIntent:
    return QueryIntent(
        is_financial_question=True,
        category=category,
        date_range=args.date_range,
        custom_start=args.custom_start,
        custom_end=args.custom_end,
        aggregation=aggregation,
        intent_summary="",
    )


# --- read tools -------------------------------------------------------------


async def get_month_plan_tool(ctx: ToolContext, args: NoArgs) -> dict:
    plan = await get_month_plan(ctx.session, ctx.user_id)
    return {
        "salary": str(plan.salary),
        "other_income": str(plan.other_income),
        "moved_to_savings": str(plan.moved_to_savings),
        "taken_back_from_savings": str(plan.taken_back_from_savings),
        "saved_this_month": str(plan.saved),
        "saved_pct_of_salary": None if plan.saved_pct is None else str(plan.saved_pct),
        "spending_money": str(plan.spending_money),
        "spent": str(plan.spent),
        "money_left": str(plan.money_left),
        "run_out_date": None if plan.run_out_date is None else plan.run_out_date.isoformat(),
        "summary": plan.as_message(),
    }


async def get_spending(ctx: ToolContext, args: SpendingArgs) -> dict:
    category = normalize_category(args.category) if args.category else None
    result = await run_query(ctx.session, ctx.user_id, _intent(args, "sum", category))
    return {
        "from": result.start.isoformat(),
        "to": result.end.isoformat(),
        "category": category or "all",
        "total_spent": str(result.total),
        "number_of_expenses": result.count,
        "summary": result.as_message(),
    }


async def get_balance(ctx: ToolContext, args: PeriodArgs) -> dict:
    result = await run_query(ctx.session, ctx.user_id, _intent(args, "net", None))
    return {
        "from": result.start.isoformat(),
        "to": result.end.isoformat(),
        "income": str(result.net_income),
        "spent": str(result.net_spend),
        "moved_to_savings": str(result.net_moved_to_savings),
        "left": str(result.net_income - result.net_spend),
        "summary": result.as_message(),
    }


async def list_expenses(ctx: ToolContext, args: SpendingArgs) -> dict:
    """The 15 most recent expenses in the period (with ids, so the model can
    propose recategorizing one)."""
    result = await run_query(ctx.session, ctx.user_id, _intent(args, "sum", None))
    query = (
        select(Expense)
        .where(Expense.user_id == ctx.user_id, Expense.txn_date >= result.start, Expense.txn_date <= result.end)
        .order_by(Expense.txn_date.desc())
        .limit(15)
    )
    category = normalize_category(args.category) if args.category else None
    if category:
        query = query.where(func.lower(Expense.category) == category.lower())
    expenses = (await ctx.session.execute(query)).scalars().all()
    items = []
    for expense in expenses:
        ctx.seen_expense_ids.add(str(expense.id))
        items.append(
            {
                "id": str(expense.id),
                "date": expense.txn_date.isoformat(),
                "amount": str(expense.amount),
                "category": expense.category,
                "merchant": expense.merchant,
                "notes": expense.notes,
            }
        )
    return {"expenses": items}


async def get_budgets(ctx: ToolContext, args: NoArgs) -> dict:
    statuses = await get_budget_statuses(ctx.session, ctx.user_id)
    return {
        "budgets": [
            {"category": s.category, "monthly_limit": str(s.monthly_limit), "spent_this_month": str(s.spent)}
            for s in statuses
        ]
    }


async def get_salary_settings(ctx: ToolContext, args: NoArgs) -> dict:
    profile = await get_salary_profile(ctx.session, ctx.user_id)
    if profile is None:
        return {"salary_profile": None}
    return {
        "expected_salary": str(profile.expected_salary),
        "savings_goal": None if profile.savings_target is None else str(profile.savings_target),
        "payday": profile.payday_day,
    }


# --- write tools (always confirmed by the user) -----------------------------


class SetBudgetArgs(BaseModel):
    category: str = Field(description=f"One of: {', '.join(CATEGORY_NAMES)}")
    monthly_limit: Decimal = Field(description="Rupees per month")


class RecategorizeArgs(BaseModel):
    expense_id: str = Field(description="id from list_expenses")
    category: str = Field(description=f"One of: {', '.join(CATEGORY_NAMES)}")


class LogCashArgs(BaseModel):
    category: str = Field(description=f"One of: {', '.join(CATEGORY_NAMES)}")
    amount: Decimal = Field(description="Rupees")
    note: str | None = Field(default=None, description="Short note, e.g. 'lunch'")


class SavingsAccountArgs(BaseModel):
    last4: str = Field(description="Last 4 digits of the user's own savings account")


def describe_action(tool_name: str, args: BaseModel) -> str:
    """The Yes/No question shown to the user for a proposed change."""
    if isinstance(args, SetBudgetArgs):
        return f"Set your {normalize_category(args.category)} budget to Rs.{args.monthly_limit:,.2f} a month?"
    if isinstance(args, RecategorizeArgs):
        return f"Move this expense to {normalize_category(args.category)}?"
    if isinstance(args, LogCashArgs):
        note = f" ({args.note})" if args.note else ""
        return f"Log a cash expense of Rs.{args.amount:,.2f} under {normalize_category(args.category)}{note}?"
    if isinstance(args, SavingsAccountArgs):
        return f"Remember account ending {args.last4} as your own savings account?"
    return f"Run {tool_name}?"


# Which tool each argument model belongs to.
TOOL_NAME_FOR_ARGS: dict[type[BaseModel], str] = {
    SetBudgetArgs: "set_budget",
    RecategorizeArgs: "recategorize_expense",
    LogCashArgs: "log_cash_expense",
    SavingsAccountArgs: "remember_savings_account",
}


async def propose_change(ctx: ToolContext, args: BaseModel, reason: str) -> dict:
    """The `ask_instead` of every chat write tool: store the change as a
    PendingAction; the user confirms it with Yes/No buttons."""
    tool_name = TOOL_NAME_FOR_ARGS[type(args)]
    action = PendingAction(
        user_id=ctx.user_id,
        tool_name=tool_name,
        args=args.model_dump_json(),
        question=describe_action(tool_name, args),
        status="waiting",
    )
    ctx.session.add(action)
    await ctx.session.commit()
    ctx.confirmations.append(action)
    return {"status": "waiting_for_user_to_confirm", "question_shown_to_user": action.question}


async def _never_runs_directly(ctx: ToolContext, args: BaseModel) -> dict:
    # The policy always answers "ask" for chat writes, so this never runs.
    raise RuntimeError("chat write tools only run after the user confirms")


def _read_tool(name: str, description: str, args_model: type[BaseModel], run) -> Tool:  # type: ignore[no-untyped-def]
    return Tool(name=name, description=description, kind="read", args_model=args_model, run=run)


def _write_tool(name: str, description: str, args_model: type[BaseModel]) -> Tool:
    return Tool(
        name=name,
        description=description + " The user must confirm with a button before anything changes.",
        kind="write",
        args_model=args_model,
        run=_never_runs_directly,
        ask_instead=propose_change,
    )


CHAT_TOOLS = [
    _read_tool(
        "get_month_plan",
        "This month: salary, money moved to / taken back from savings, spending money, spent, money left.",
        NoArgs,
        get_month_plan_tool,
    ),
    _read_tool("get_spending", "Total spent in a period, optionally for one category.", SpendingArgs, get_spending),
    _read_tool("get_balance", "Income minus spending for a period ('how much is left').", PeriodArgs, get_balance),
    _read_tool("list_expenses", "The most recent expenses in a period, with their ids.", SpendingArgs, list_expenses),
    _read_tool("get_budgets", "Monthly budgets and this month's spend per category.", NoArgs, get_budgets),
    _read_tool("get_salary_settings", "Expected salary, optional savings goal, payday.", NoArgs, get_salary_settings),
    _write_tool("set_budget", "Set a monthly budget for a category.", SetBudgetArgs),
    _write_tool("recategorize_expense", "Change the category of an expense from list_expenses.", RecategorizeArgs),
    _write_tool("log_cash_expense", "Log a cash expense that has no bank SMS.", LogCashArgs),
    _write_tool(
        "remember_savings_account", "Remember the last 4 digits of the user's savings account.", SavingsAccountArgs
    ),
]


def action_args(action: PendingAction) -> dict:
    return json.loads(action.args)
