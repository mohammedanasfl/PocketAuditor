"""This month's money plan, measured from what actually happened.

The amount you save changes every month, so nothing here assumes a fixed
percentage. Everything comes from one formula (docs/AGENT_DESIGN.md §8):

    spending_money = salary + other_income
                   − moved_to_savings        (transfers to your own savings account)
                   + taken_back_from_savings  (withdrawals from it)

    money_left     = spending_money − spent_so_far

All numbers are computed here in plain code. The agent and the alerts only
ever read them.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.categories import SAVINGS_CATEGORY
from app.db import insert_once
from app.models import AuditAlertSent, Expense, Income
from app.savings import SAVINGS_WITHDRAWAL
from app.timeutil import local_today

# Don't project a run-out date from fewer days than this — one big purchase
# on day 2 shouldn't trigger a false alarm.
_MIN_DAY_FOR_PACE_ALERT = 7


@dataclass
class MonthPlan:
    month_start: date
    today: date
    salary: Decimal
    other_income: Decimal
    moved_to_savings: Decimal
    taken_back_from_savings: Decimal
    spent: Decimal

    @property
    def spending_money(self) -> Decimal:
        return self.salary + self.other_income - self.moved_to_savings + self.taken_back_from_savings

    @property
    def money_left(self) -> Decimal:
        return self.spending_money - self.spent

    @property
    def saved(self) -> Decimal:
        """What stayed in savings this month."""
        return self.moved_to_savings - self.taken_back_from_savings

    @property
    def saved_pct(self) -> Decimal | None:
        """Saved as a % of salary, or None before the salary arrives."""
        if self.salary <= 0:
            return None
        return (self.saved / self.salary * 100).quantize(Decimal("0.1"))

    @property
    def days_in_month(self) -> int:
        return calendar.monthrange(self.today.year, self.today.month)[1]

    @property
    def run_out_date(self) -> date | None:
        """When money_left hits zero at the average daily spend so far.
        None if nothing was spent yet, or it lasts past the month end."""
        if self.spent <= 0 or self.money_left <= 0:
            return None
        daily_spend = self.spent / self.today.day
        days_left = int(self.money_left / daily_spend)
        run_out = self.today + timedelta(days=days_left)
        month_end = self.month_start.replace(day=self.days_in_month)
        return run_out if run_out < month_end else None

    def as_message(self) -> str:
        lines = [f"📅 {calendar.month_name[self.month_start.month]} so far"]
        lines.append(f"Salary: Rs.{self.salary:,.2f}")
        if self.other_income > 0:
            lines.append(f"Other income: Rs.{self.other_income:,.2f}")
        if self.moved_to_savings > 0:
            pct = f" ({self.saved_pct}% of salary kept)" if self.saved_pct is not None else ""
            lines.append(f"Moved to savings: Rs.{self.moved_to_savings:,.2f}")
            if self.taken_back_from_savings > 0:
                lines.append(f"Taken back from savings: Rs.{self.taken_back_from_savings:,.2f}")
            lines.append(f"Saved this month: Rs.{self.saved:,.2f}{pct}")
        else:
            lines.append("Nothing moved to savings yet this month.")
        lines.append(f"Spending money: Rs.{self.spending_money:,.2f}")
        lines.append(f"Spent: Rs.{self.spent:,.2f}")
        lines.append(f"Money left: Rs.{self.money_left:,.2f}")
        if self.run_out_date is not None:
            lines.append(f"⚠️ At this pace you run out around {self.run_out_date:%d %b}.")
        return "\n".join(lines)


async def _income_sum(session: AsyncSession, user_id: UUID, start: date, end: date, kinds: list[str]) -> Decimal:
    """Total of this user's incomes of the given kinds between two dates."""
    query = select(func.coalesce(func.sum(Income.amount), 0)).where(
        Income.user_id == user_id,
        Income.txn_date >= start,
        Income.txn_date <= end,
        Income.kind.in_(kinds),
    )
    return Decimal(str((await session.execute(query)).scalar_one()))


async def _expense_sum(session: AsyncSession, user_id: UUID, start: date, end: date, *, savings: bool) -> Decimal:
    """Total of this user's expenses between two dates — either only the
    Savings category (savings=True) or everything else (savings=False)."""
    is_savings = func.lower(Expense.category) == SAVINGS_CATEGORY.lower()
    query = select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.user_id == user_id,
        Expense.txn_date >= start,
        Expense.txn_date <= end,
        is_savings if savings else ~is_savings,
    )
    return Decimal(str((await session.execute(query)).scalar_one()))


async def get_month_plan(session: AsyncSession, user_id: UUID, *, today: date | None = None) -> MonthPlan:
    today = today or local_today()
    start = today.replace(day=1)
    return MonthPlan(
        month_start=start,
        today=today,
        salary=await _income_sum(session, user_id, start, today, ["salary"]),
        other_income=await _income_sum(session, user_id, start, today, ["other", "refund"]),
        moved_to_savings=await _expense_sum(session, user_id, start, today, savings=True),
        taken_back_from_savings=await _income_sum(session, user_id, start, today, [SAVINGS_WITHDRAWAL]),
        spent=await _expense_sum(session, user_id, start, today, savings=False),
    )


@dataclass
class MoneyAlert:
    alert_type: str  # 'half_used' | 'running_low' | 'ran_out' | 'fast_pace'
    message: str

    def as_message(self) -> str:
        return self.message


def money_alerts_for(plan: MonthPlan) -> list[MoneyAlert]:
    """Which alerts this plan deserves (before checking what was already sent)."""
    if plan.spending_money <= 0:
        return []  # no salary/income recorded yet — nothing to measure against

    alerts: list[MoneyAlert] = []
    used = plan.spent / plan.spending_money
    if plan.money_left <= 0:
        alerts.append(
            MoneyAlert(
                "ran_out",
                f"🚨 You've used all your spending money for this month (Rs.{plan.spending_money:,.2f}). "
                f"You're Rs.{-plan.money_left:,.2f} over. If you take money from savings, forward that SMS "
                "so I can track it.",
            )
        )
    elif plan.money_left <= plan.spending_money * Decimal("0.2"):
        alerts.append(
            MoneyAlert(
                "running_low",
                f"⚠️ Only Rs.{plan.money_left:,.2f} of your Rs.{plan.spending_money:,.2f} spending money is left.",
            )
        )
    elif used >= Decimal("0.5"):
        alerts.append(
            MoneyAlert(
                "half_used",
                f"ℹ️ You've used half your spending money: Rs.{plan.spent:,.2f} of Rs.{plan.spending_money:,.2f}.",
            )
        )

    if plan.today.day >= _MIN_DAY_FOR_PACE_ALERT and plan.run_out_date is not None:
        alerts.append(
            MoneyAlert(
                "fast_pace",
                f"📈 At your current pace your spending money runs out around {plan.run_out_date:%d %b}, "
                "before the month ends.",
            )
        )
    return alerts


async def check_money_alerts(session: AsyncSession, user_id: UUID, *, today: date | None = None) -> list[MoneyAlert]:
    """Alerts to send now — each type at most once per month (deduped with
    audit_alerts_sent, the same way the other alerts are)."""
    plan = await get_month_plan(session, user_id, today=today)
    to_send: list[MoneyAlert] = []
    for alert in money_alerts_for(plan):
        marker = AuditAlertSent(user_id=user_id, month=plan.month_start, alert_type=alert.alert_type)
        if await insert_once(session, marker):
            to_send.append(alert)
    return to_send
