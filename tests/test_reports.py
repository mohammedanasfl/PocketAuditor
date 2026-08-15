"""Tests for /spend's underlying spend summary — week/month/year/all-time
totals computed from the expenses ledger."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.models import Expense, User
from app.reports import get_spend_summary


async def _make_user(session) -> User:
    user = User(telegram_chat_id=54321)
    session.add(user)
    await session.commit()
    return user


async def _make_expense(session, user: User, *, amount: str, txn_date: date) -> Expense:
    expense = Expense(user_id=user.id, amount=Decimal(amount), category="Other", txn_date=txn_date)
    session.add(expense)
    await session.commit()
    return expense


async def test_spend_summary_buckets_by_period(db_session):
    user = await _make_user(db_session)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)

    # Inside this week (and therefore this month and this year too)
    await _make_expense(db_session, user, amount="100.00", txn_date=today)

    # Earlier this month, but before this week started (skip if the month
    # just started and there's no earlier-this-month day available)
    if week_start > month_start:
        await _make_expense(db_session, user, amount="50.00", txn_date=week_start - timedelta(days=1))
        expected_week = Decimal("100.00")
        expected_month = Decimal("150.00")
    else:
        expected_week = Decimal("100.00")
        expected_month = Decimal("100.00")

    # Earlier this year, but before this month started (skip in January)
    if month_start > year_start:
        await _make_expense(db_session, user, amount="25.00", txn_date=month_start - timedelta(days=1))
        expected_year = expected_month + Decimal("25.00")
    else:
        expected_year = expected_month

    # Last year — should only ever land in "all time"
    await _make_expense(db_session, user, amount="10.00", txn_date=year_start - timedelta(days=1))
    expected_total = expected_year + Decimal("10.00")

    summary = await get_spend_summary(db_session, user.id)

    assert summary.week == expected_week
    assert summary.month == expected_month
    assert summary.year == expected_year
    assert summary.total == expected_total


async def test_spend_summary_zero_when_no_expenses(db_session):
    user = await _make_user(db_session)
    summary = await get_spend_summary(db_session, user.id)
    assert summary.week == summary.month == summary.year == summary.total == Decimal("0")


async def test_spend_summary_scopes_to_the_requesting_user(db_session):
    user_a = await _make_user(db_session)
    user_b = User(telegram_chat_id=99999)
    db_session.add(user_b)
    await db_session.commit()

    await _make_expense(db_session, user_a, amount="100.00", txn_date=date.today())
    await _make_expense(db_session, user_b, amount="500.00", txn_date=date.today())

    summary = await get_spend_summary(db_session, user_a.id)
    assert summary.total == Decimal("100.00")


def test_as_message_formats_all_four_periods():
    from app.reports import SpendSummary

    summary = SpendSummary(
        week=Decimal("100.00"), month=Decimal("1234.50"), year=Decimal("50000.00"), total=Decimal("99999.99")
    )
    text = summary.as_message()
    assert "This week: Rs.100.00" in text
    assert "This month: Rs.1,234.50" in text
    assert "This year: Rs.50,000.00" in text
    assert "All time: Rs.99,999.99" in text
