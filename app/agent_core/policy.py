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
    return Verdict("deny", f"no policy rule for {tool_name}, so it is not allowed")


async def _check_log_expense(args: LogExpenseArgs, ctx: ToolContext) -> Verdict:
    txn = ctx.transaction
    assert txn is not None
    category = normalize_category(args.category)

    if args.confidence < settings.confidence_threshold:
        return Verdict("ask", f"confidence {args.confidence:.2f} is below {settings.confidence_threshold}")
    if category is None:
        return Verdict("ask", f"{args.category!r} is not one of the known categories")
    if is_savings_category(category):
        # Only the own-account detector (plain code) may file money under
        # Savings — never the model, e.g. because an SMS told it to.
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
