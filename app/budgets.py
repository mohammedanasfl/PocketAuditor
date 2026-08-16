"""Phase 3a: deterministic budget-threshold alerts. No LLM anywhere in this
module — pure SQL aggregation compared against `budgets.monthly_limit`.

Reads from `expenses` (the confirmed ledger), same as app/reports.py, not
`transactions` — a transaction only counts toward spend once it's actually
become an expense.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Budget, BudgetAlertSent, Expense

_ALERT_THRESHOLD_PCT = Decimal("0.8")


@dataclass
class BudgetAlert:
    """One category crossing its alert threshold for the current month."""

    category: str
    spent: Decimal
    monthly_limit: Decimal
    days_left: int

    def as_message(self) -> str:
        pct = round(self.spent / self.monthly_limit * 100)
        day_word = "day" if self.days_left == 1 else "days"
        return (
            f"⚠️ {self.category}: Rs.{self.spent:,.2f} of Rs.{self.monthly_limit:,.2f} "
            f"budget ({pct}%) — {self.days_left} {day_word} left this month"
        )


@dataclass
class BudgetStatus:
    """One category's current-month limit vs. spend, for /budgets."""

    category: str
    monthly_limit: Decimal
    spent: Decimal

    def as_line(self) -> str:
        pct = round(self.spent / self.monthly_limit * 100) if self.monthly_limit else 0
        return f"{self.category}: Rs.{self.spent:,.2f} / Rs.{self.monthly_limit:,.2f} ({pct}%)"


def format_budgets_message(statuses: list[BudgetStatus]) -> str:
    if not statuses:
        return "No budgets set yet — use /setbudget <category> <amount> to add one."
    lines = ["📊 Budgets this month:"] + [status.as_line() for status in statuses]
    return "\n".join(lines)


async def upsert_budget(session: AsyncSession, user_id: UUID, category: str, monthly_limit: Decimal) -> Budget:
    """Create or update the (user_id, category) budget row."""
    stmt = select(Budget).where(Budget.user_id == user_id, Budget.category == category)
    budget = (await session.execute(stmt)).scalar_one_or_none()
    if budget is None:
        budget = Budget(user_id=user_id, category=category, monthly_limit=monthly_limit)
        session.add(budget)
    else:
        budget.monthly_limit = monthly_limit
    await session.commit()
    return budget


async def _month_spend_by_category(session: AsyncSession, user_id: UUID, month_start: date) -> dict[str, Decimal]:
    """Keyed by lowercased category. expenses.category isn't a strict enum —
    an auto_log category can come from the LLM's own guess or a category_hint
    and isn't guaranteed to match /setbudget's exact casing — so matching
    must be case-insensitive here too, same as app/query.py's /ask filter."""
    stmt = (
        select(func.lower(Expense.category), func.sum(Expense.amount))
        .where(Expense.user_id == user_id, Expense.txn_date >= month_start)
        .group_by(func.lower(Expense.category))
    )
    rows = (await session.execute(stmt)).all()
    return {category: Decimal(str(spent)) for category, spent in rows}


async def get_budget_statuses(session: AsyncSession, user_id: UUID, *, today: date | None = None) -> list[BudgetStatus]:
    today = today or date.today()
    month_start = today.replace(day=1)

    budgets = (
        (await session.execute(select(Budget).where(Budget.user_id == user_id).order_by(Budget.category)))
        .scalars()
        .all()
    )
    if not budgets:
        return []

    spend_by_category = await _month_spend_by_category(session, user_id, month_start)
    return [
        BudgetStatus(
            category=budget.category,
            monthly_limit=budget.monthly_limit,
            spent=spend_by_category.get(budget.category.lower(), Decimal("0")),
        )
        for budget in budgets
    ]


async def check_budget_alerts(session: AsyncSession, user_id: UUID, *, today: date | None = None) -> list[BudgetAlert]:
    """Compares this month's spend per category against each budget's
    monthly_limit, firing at most one alert per category per calendar month.

    A category with expenses but no budget row is naturally skipped — this
    only ever iterates rows that exist in `budgets`, never all categories
    seen in `expenses`.
    """
    today = today or date.today()
    month_start = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_left = days_in_month - today.day

    budgets = (await session.execute(select(Budget).where(Budget.user_id == user_id))).scalars().all()
    if not budgets:
        return []

    spend_by_category = await _month_spend_by_category(session, user_id, month_start)

    alerts: list[BudgetAlert] = []
    for budget in budgets:
        if budget.monthly_limit <= 0:
            continue  # degenerate config — nothing sensible to alert against

        spent = spend_by_category.get(budget.category.lower(), Decimal("0"))
        if spent / budget.monthly_limit < _ALERT_THRESHOLD_PCT:
            continue

        already_sent = (
            await session.execute(
                select(BudgetAlertSent).where(
                    BudgetAlertSent.user_id == user_id,
                    BudgetAlertSent.category == budget.category,
                    BudgetAlertSent.month == month_start,
                )
            )
        ).scalar_one_or_none()
        if already_sent is not None:
            continue

        session.add(BudgetAlertSent(user_id=user_id, category=budget.category, month=month_start))
        await session.commit()
        alerts.append(
            BudgetAlert(category=budget.category, spent=spent, monthly_limit=budget.monthly_limit, days_left=days_left)
        )

    return alerts
