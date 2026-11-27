"""The policy layer: checks every write tool call BEFORE it runs.

The model proposes, this code decides. Three possible verdicts:

- allow — run the tool.
- ask   — don't run it; ask the user instead (the tool's `ask_instead`).
- deny  — don't run it; tell the model why (e.g. it made up an id).

These are the same rules the original pipeline enforced in
app/agent.py:_apply_guard / _apply_duplicate_guard, written as one plain
function per tool so each rule is easy to read and test.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from app.agent import find_recent_duplicate_expense
from app.agent_core.tools import LinkExpenseArgs, LogExpenseArgs, ToolContext
from app.categories import is_savings_category, normalize_category
from app.config import settings


@dataclass
class Verdict:
    decision: Literal["allow", "ask", "deny"]
    reason: str = ""


ALLOW = Verdict("allow")


async def check(tool_name: str, args: Any, ctx: ToolContext) -> Verdict:
    """Decide whether one write tool call may run."""
    # Reconcile writes: one decision per transaction, and only while it's pending.
    if tool_name in ("log_as_new_expense", "link_to_expense", "ask_user_for_category"):
        if ctx.transaction is None:
            return Verdict("deny", "this tool only works while processing a transaction")
        if ctx.outcome is not None or ctx.transaction.status != "pending":
            return Verdict("deny", "this transaction has already been handled")

    if tool_name == "log_as_new_expense":
        return await _check_log_expense(args, ctx)
    if tool_name == "link_to_expense":
        return _check_link_expense(args, ctx)
    if tool_name == "ask_user_for_category":
        return ALLOW  # asking is always the safe choice

    # Chat writes: check the values, then ALWAYS ask the user to confirm.
    if tool_name in ("set_budget", "recategorize_expense", "log_cash_expense", "remember_savings_account"):
        problem = _chat_write_problem(tool_name, args, ctx)
        if problem:
            return Verdict("deny", problem)
        return Verdict("ask", "changes from chat are always confirmed by the user")

    return Verdict("deny", f"no policy rule for {tool_name}, so it is not allowed")


# The biggest amount we accept from chat (Rs.1 crore) — anything above is a typo.
_MAX_CHAT_AMOUNT = Decimal("10000000")


def _chat_write_problem(tool_name: str, args: Any, ctx: ToolContext) -> str | None:
    """Why a proposed chat change is invalid, or None if it looks fine."""
    category = getattr(args, "category", None)
    if category is not None and normalize_category(category) is None:
        return f"{category!r} is not one of the known categories"
    amount = None
    if hasattr(args, "monthly_limit"):
        amount = args.monthly_limit
    elif hasattr(args, "amount"):
        amount = args.amount
    if amount is not None and not (Decimal("0") < amount <= _MAX_CHAT_AMOUNT):
        return "the amount must be more than 0 and at most Rs.1,00,00,000"
    if tool_name == "recategorize_expense" and args.expense_id not in ctx.seen_expense_ids:
        return "that expense id was never shown to you — call list_expenses first"
    if tool_name == "remember_savings_account" and not (len(args.last4) == 4 and args.last4.isdigit()):
        return "last4 must be exactly 4 digits"
    return None


async def _check_log_expense(args: LogExpenseArgs, ctx: ToolContext) -> Verdict:
    txn = ctx.transaction
    assert txn is not None
    category = normalize_category(args.category)

    if args.confidence < settings.confidence_threshold:
        return Verdict("ask", f"confidence {args.confidence:.2f} is below {settings.confidence_threshold}")
    if category is None:
        return Verdict("ask", f"{args.category!r} is not one of the known categories")
    if is_savings_category(category) and txn.category_hint != category:
        # Only plain code (the own-account detector in app/ingestion.py) or
        # the user's own caption may file money under Savings — never the
        # model on its own, e.g. because an SMS told it to.
        return Verdict("ask", "only transfers to your own savings account are filed under Savings automatically")
    if txn.category_hint and category != txn.category_hint:
        return Verdict("ask", f"you captioned this {txn.category_hint!r} but the model chose {category!r}")
    if txn.source == "photo" and not txn.category_hint:
        return Verdict("ask", "a receipt photo without a caption is too ambiguous to log on its own")
    if txn.merchant:
        duplicate = await find_recent_duplicate_expense(ctx.session, ctx.user_id, txn)
        if duplicate is not None:
            return Verdict(
                "ask", f"possible double charge: Rs.{duplicate.amount} at {duplicate.merchant} on {duplicate.txn_date}"
            )
    return ALLOW


def _check_link_expense(args: LinkExpenseArgs, ctx: ToolContext) -> Verdict:
    if args.expense_id not in ctx.seen_expense_ids:
        return Verdict("deny", "that expense id was never shown to you — call find_matching_expenses first")
    if args.confidence < settings.confidence_threshold:
        return Verdict("ask", f"confidence {args.confidence:.2f} is below {settings.confidence_threshold}")
    return ALLOW
