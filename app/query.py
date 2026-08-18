"""Phase 3b: turns a validated QueryIntent — never raw SQL, never the user's
raw question text — into a parameterized SQLAlchemy query against
`expenses`, then phrases the answer via plain string formatting.

Deliberately NOT another LLM call for phrasing: the numbers in the reply must
be exactly what the query returned, and an LLM re-stating them is a chance
(however small) to transcribe them wrong. Same reasoning as agent.py's
_apply_guard re-checking the model's own decision in code rather than
trusting it — anything that must be exactly right is computed, not phrased
by a model.

Phase 4 widened this beyond `expenses` alone: aggregation="net" answers
"how much money do I have left" style questions by also summing `incomes`
for the period. Savings-category expenses are excluded from that sum, same
as app/reports.py and app/audit.py — money moved to another account isn't
spent, so counting it as spend would understate the balance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.categories import SAVINGS_CATEGORY
from app.models import Expense, Income
from app.schemas import QueryIntent

logger = logging.getLogger(__name__)

_LIST_LIMIT = 10

_NAMED_RANGE_PHRASES = {
    "today": "today",
    "this_week": "this week",
    "last_week": "last week",
    "this_month": "this month",
    "last_month": "last month",
}


@dataclass
class QueryResult:
    intent: QueryIntent
    start: date
    end: date
    count: int = 0
    total: Decimal = Decimal("0")
    max_item: dict | None = None
    items: list[dict] = field(default_factory=list)
    truncated: bool = False
    # Only populated for aggregation="net" — income/spend are always
    # well-defined (even both zero), so "net" never needs the is_empty path.
    net_income: Decimal = Decimal("0")
    net_spend: Decimal = Decimal("0")
    net_moved_to_savings: Decimal = Decimal("0")

    @property
    def is_empty(self) -> bool:
        return self.count == 0

    def _period_phrase(self) -> str:
        if self.intent.date_range != "custom":
            return _NAMED_RANGE_PHRASES[self.intent.date_range]
        if self.start == self.end:
            return f"on {self.start}"
        return f"from {self.start} to {self.end}"

    def _net_message(self) -> str:
        net = self.net_income - self.net_spend
        savings_note = (
            f", Rs.{self.net_moved_to_savings:,.2f} moved to savings not counted"
            if self.net_moved_to_savings > 0
            else ""
        )
        detail = f"(Rs.{self.net_income:,.2f} income − Rs.{self.net_spend:,.2f} spent{savings_note})"
        period_phrase = self._period_phrase()
        if net >= 0:
            return f"You have Rs.{net:,.2f} left {period_phrase} {detail}."
        return f"You've spent Rs.{-net:,.2f} more than you earned {period_phrase} {detail}."

    def as_message(self) -> str:
        if self.intent.aggregation == "net":
            return self._net_message()
        if self.is_empty:
            return "No expenses found for that period."

        category_phrase = f" on {self.intent.category}" if self.intent.category else ""
        period_phrase = self._period_phrase()

        if self.intent.aggregation == "sum":
            txn_word = "transaction" if self.count == 1 else "transactions"
            return f"You spent Rs.{self.total:,.2f}{category_phrase} {period_phrase} across {self.count} {txn_word}."

        if self.intent.aggregation == "count":
            txn_word = "transaction" if self.count == 1 else "transactions"
            return f"You made {self.count} {txn_word}{category_phrase} {period_phrase}."

        if self.intent.aggregation == "max":
            # run_query only ever leaves max_item unset when count == 0, which
            # the is_empty check above has already returned early for.
            assert self.max_item is not None
            item = self.max_item
            merchant_phrase = f" at {item['merchant']}" if item["merchant"] else ""
            return (
                f"Your biggest expense{category_phrase} {period_phrase} was "
                f"Rs.{item['amount']:,.2f}{merchant_phrase} on {item['txn_date']}."
            )

        # "list"
        lines = [
            f"Rs.{i['amount']:,.2f} — {i['merchant'] or 'unknown merchant'} ({i['category']}, {i['txn_date']})"
            for i in self.items
        ]
        text = f"Expenses{category_phrase} {period_phrase}:\n" + "\n".join(lines)
        if self.truncated:
            text += f"\n…and {self.count - len(self.items)} more — showing the first {_LIST_LIMIT}."
        return text


def resolve_date_range(intent: QueryIntent, today: date) -> tuple[date, date]:
    """Deterministic — never trusts the LLM's own date arithmetic. Only
    intent.date_range (a closed enum) and, for "custom", the two explicit
    dates it gave are ever used."""
    if intent.date_range == "today":
        return today, today
    if intent.date_range == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, today
    if intent.date_range == "last_week":
        this_week_start = today - timedelta(days=today.weekday())
        return this_week_start - timedelta(days=7), this_week_start - timedelta(days=1)
    if intent.date_range == "this_month":
        return today.replace(day=1), today
    if intent.date_range == "last_month":
        this_month_start = today.replace(day=1)
        last_month_end = this_month_start - timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end

    # "custom"
    if intent.custom_start is None or intent.custom_end is None:
        logger.warning("interpret_query returned date_range=custom without both dates — falling back to this_month")
        return today.replace(day=1), today
    return intent.custom_start, intent.custom_end


async def _run_net_query(
    session: AsyncSession, user_id: UUID, intent: QueryIntent, start: date, end: date
) -> QueryResult:
    """aggregation="net": income minus spend for the period, spanning both
    ledgers rather than filtering `expenses` alone. `category` is ignored
    here regardless of what the model set — a balance isn't category-scoped."""
    income_total = Decimal(
        str(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Income.amount), 0)).where(
                        Income.user_id == user_id, Income.txn_date >= start, Income.txn_date <= end
                    )
                )
            ).scalar_one()
        )
    )
    spend_total = Decimal(
        str(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Expense.amount), 0)).where(
                        Expense.user_id == user_id,
                        Expense.txn_date >= start,
                        Expense.txn_date <= end,
                        func.lower(Expense.category) != SAVINGS_CATEGORY.lower(),
                    )
                )
            ).scalar_one()
        )
    )
    moved_to_savings = Decimal(
        str(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Expense.amount), 0)).where(
                        Expense.user_id == user_id,
                        Expense.txn_date >= start,
                        Expense.txn_date <= end,
                        func.lower(Expense.category) == SAVINGS_CATEGORY.lower(),
                    )
                )
            ).scalar_one()
        )
    )
    return QueryResult(
        intent=intent,
        start=start,
        end=end,
        net_income=income_total,
        net_spend=spend_total,
        net_moved_to_savings=moved_to_savings,
    )


async def run_query(
    session: AsyncSession, user_id: UUID, intent: QueryIntent, *, today: date | None = None
) -> QueryResult:
    """The one place free-text ever stops: `intent` is a validated
    QueryIntent, never the user's raw question. Every value bound into the
    query below (category, date bounds) goes through SQLAlchemy's parameter
    binding, never string concatenation — so nothing in `intent.category`,
    however it's spelled, can affect the query's structure."""
    today = today or date.today()
    start, end = resolve_date_range(intent, today)

    if intent.aggregation == "net":
        return await _run_net_query(session, user_id, intent, start, end)

    stmt = select(Expense).where(
        Expense.user_id == user_id,
        Expense.txn_date >= start,
        Expense.txn_date <= end,
    )
    if intent.category:
        stmt = stmt.where(func.lower(Expense.category) == intent.category.lower())

    expenses = (await session.execute(stmt.order_by(Expense.txn_date))).scalars().all()

    result = QueryResult(intent=intent, start=start, end=end, count=len(expenses))
    if not expenses:
        return result

    result.total = sum((expense.amount for expense in expenses), Decimal("0"))

    if intent.aggregation == "max":
        biggest = max(expenses, key=lambda expense: expense.amount)
        result.max_item = {
            "amount": biggest.amount,
            "merchant": biggest.merchant,
            "category": biggest.category,
            "txn_date": biggest.txn_date,
        }
    elif intent.aggregation == "list":
        capped = expenses[:_LIST_LIMIT]
        result.items = [
            {
                "amount": expense.amount,
                "merchant": expense.merchant,
                "category": expense.category,
                "txn_date": expense.txn_date,
            }
            for expense in capped
        ]
        result.truncated = len(expenses) > _LIST_LIMIT

    return result
