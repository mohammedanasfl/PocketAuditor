"""Income-side service functions: recording a money-in ledger row (a forwarded
SMS credit) and summarizing income for /income.

Kept separate from app.expenses / app.ingestion on purpose: a credit never
becomes a `transactions` row and never enters app.agent.reconcile_user — it's
written straight into `incomes` at ingestion, since there's no manually-kept
income ledger to reconcile it against. /spend and the budget aggregation read
`expenses` only, so income stays out of them entirely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Income

logger = logging.getLogger(__name__)


async def record_income(
    session: AsyncSession,
    user_id: UUID,
    *,
    amount: Decimal,
    source: str | None,
    txn_date: date,
    raw_text: str,
    created_via: str = "auto",
) -> Income:
    """Write a single money-in row. Mirrors app.expenses.log_manual_expense,
    but for the `incomes` ledger — no transaction/reconciliation behind it."""
    income = Income(
        user_id=user_id,
        amount=amount,
        source=source,
        txn_date=txn_date,
        raw_text=raw_text,
        created_via=created_via,
    )
    session.add(income)
    await session.commit()
    logger.info("recorded income=%s user=%s amount=%s source=%r", income.id, user_id, amount, source)
    return income


@dataclass
class IncomeSummary:
    month: Decimal
    last_month: Decimal
    year: Decimal
    total: Decimal

    def as_message(self) -> str:
        return (
            "💵 Income summary\n"
            f"This month: Rs.{self.month:,.2f}\n"
            f"Last month: Rs.{self.last_month:,.2f}\n"
            f"This year: Rs.{self.year:,.2f}\n"
            f"All time: Rs.{self.total:,.2f}"
        )


async def _sum_between(session: AsyncSession, user_id: UUID, start: date | None, end: date | None) -> Decimal:
    stmt = select(func.coalesce(func.sum(Income.amount), 0)).where(Income.user_id == user_id)
    if start is not None:
        stmt = stmt.where(Income.txn_date >= start)
    if end is not None:
        stmt = stmt.where(Income.txn_date <= end)
    total = (await session.execute(stmt)).scalar_one()
    return Decimal(str(total))


async def get_income_summary(session: AsyncSession, user_id: UUID, *, today: date | None = None) -> IncomeSummary:
    """Income totals for the current month, the previous completed month, the
    current year, and all time. Takes an injectable `today` so tests can pin
    the clock (same convention as app/budgets.py and app/query.py)."""
    today = today or date.today()
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    year_start = today.replace(month=1, day=1)

    month = await _sum_between(session, user_id, month_start, None)
    last_month = await _sum_between(session, user_id, last_month_start, last_month_end)
    year = await _sum_between(session, user_id, year_start, None)
    total = await _sum_between(session, user_id, None, None)

    return IncomeSummary(month=month, last_month=last_month, year=year, total=total)
