"""A lightweight, proactive weekly summary — deterministic, no LLM call.

Reuses app/reports.py:get_spend_summary for week/month totals rather than
recomputing them, and adds a week-scoped top-category and biggest-single-
expense breakdown. Excludes the Savings category the same way /spend
(app/reports.py), the monthly audit and its pace_high alert (app/audit.py),
and /ask's net aggregation (app/query.py) already do — this is the fifth call
site applying that exclusion; see CLAUDE.md's "Savings is a category, not a
ledger" note before changing any one of these independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.categories import SAVINGS_CATEGORY
from app.models import Expense
from app.reports import get_spend_summary


@dataclass
class WeeklyDigest:
    week_spend: Decimal
    month_spend: Decimal
    top_category: tuple[str, Decimal] | None
    biggest_expense: dict | None

    def as_message(self) -> str:
        lines = [
            "📅 Weekly digest",
            f"This week: Rs.{self.week_spend:,.2f}",
            f"Month so far: Rs.{self.month_spend:,.2f}",
        ]
        if self.top_category is not None:
            category, amount = self.top_category
            lines.append(f"Top category this week: {category} (Rs.{amount:,.2f})")
        if self.biggest_expense is not None:
            item = self.biggest_expense
            merchant_phrase = f" at {item['merchant']}" if item["merchant"] else ""
            lines.append(f"Biggest expense this week: Rs.{item['amount']:,.2f}{merchant_phrase} ({item['category']})")
        return "\n".join(lines)


async def build_weekly_digest(session: AsyncSession, user_id: UUID, *, today: date | None = None) -> WeeklyDigest:
    today = today or date.today()
    week_start = today - timedelta(days=today.weekday())

    summary = await get_spend_summary(session, user_id, today=today)

    not_savings = func.lower(Expense.category) != SAVINGS_CATEGORY.lower()

    category_stmt = (
        select(Expense.category, func.sum(Expense.amount))
        .where(Expense.user_id == user_id, Expense.txn_date >= week_start, not_savings)
        .group_by(Expense.category)
    )
    category_rows = (await session.execute(category_stmt)).all()
    top_category = None
    if category_rows:
        category, amount = max(category_rows, key=lambda row: row[1])
        top_category = (category, Decimal(str(amount)))

    biggest_stmt = (
        select(Expense)
        .where(Expense.user_id == user_id, Expense.txn_date >= week_start, not_savings)
        .order_by(Expense.amount.desc(), Expense.id.desc())
        .limit(1)
    )
    biggest = (await session.execute(biggest_stmt)).scalar_one_or_none()
    biggest_expense = None
    if biggest is not None:
        biggest_expense = {"amount": biggest.amount, "merchant": biggest.merchant, "category": biggest.category}

    return WeeklyDigest(
        week_spend=summary.week,
        month_spend=summary.month,
        top_category=top_category,
        biggest_expense=biggest_expense,
    )
