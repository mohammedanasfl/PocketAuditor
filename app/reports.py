"""Spend summary reporting: total logged expenses over standard periods.

Reads from `expenses` — the confirmed ledger — not `transactions` (raw,
possibly still-pending SMS parses). A transaction only counts toward spend
once it's actually become an expense (auto_link, auto_log, or a manually
answered ask_user).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Expense


@dataclass
class SpendSummary:
    week: Decimal
    month: Decimal
    year: Decimal
    total: Decimal

    def as_message(self) -> str:
        return (
            "💰 Spend summary\n"
            f"This week: Rs.{self.week:,.2f}\n"
            f"This month: Rs.{self.month:,.2f}\n"
            f"This year: Rs.{self.year:,.2f}\n"
            f"All time: Rs.{self.total:,.2f}"
        )


async def _sum_since(session: AsyncSession, user_id: UUID, since: date | None) -> Decimal:
    stmt = select(func.coalesce(func.sum(Expense.amount), 0)).where(Expense.user_id == user_id)
    if since is not None:
        stmt = stmt.where(Expense.txn_date >= since)
    total = (await session.execute(stmt)).scalar_one()
    return Decimal(str(total))


async def get_spend_summary(session: AsyncSession, user_id: UUID) -> SpendSummary:
    """ "This week" is the current Monday-start week; "this month"/"this year"
    are the current calendar month/year — all relative to today."""
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)

    week = await _sum_since(session, user_id, week_start)
    month = await _sum_since(session, user_id, month_start)
    year = await _sum_since(session, user_id, year_start)
    total = await _sum_since(session, user_id, None)

    return SpendSummary(week=week, month=month, year=year, total=total)
