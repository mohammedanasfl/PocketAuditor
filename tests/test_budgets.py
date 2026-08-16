"""Stage 3a tests: deterministic budget-threshold alerts. No LLM/provider
involved anywhere here — pure SQL aggregation against a real (aiosqlite) DB.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.budgets import (
    BudgetAlert,
    check_budget_alerts,
    format_budgets_message,
    get_budget_statuses,
    upsert_budget,
)
from app.models import Budget, BudgetAlertSent, Expense, User


async def _make_user(session) -> User:
    user = User(telegram_chat_id=54321)
    session.add(user)
    await session.commit()
    return user


async def _make_expense(session, user: User, *, amount: str, category: str, txn_date: date) -> Expense:
    expense = Expense(user_id=user.id, amount=Decimal(amount), category=category, txn_date=txn_date)
    session.add(expense)
    await session.commit()
    return expense


# --- upsert_budget -----------------------------------------------------


async def test_upsert_budget_creates_then_updates(db_session):
    user = await _make_user(db_session)

    await upsert_budget(db_session, user.id, "Food", Decimal("4000.00"))
    budgets = (await db_session.execute(select(Budget).where(Budget.user_id == user.id))).scalars().all()
    assert len(budgets) == 1
    assert budgets[0].monthly_limit == Decimal("4000.00")

    await upsert_budget(db_session, user.id, "Food", Decimal("5000.00"))
    budgets = (await db_session.execute(select(Budget).where(Budget.user_id == user.id))).scalars().all()
    assert len(budgets) == 1  # updated in place, not a second row
    assert budgets[0].monthly_limit == Decimal("5000.00")


# --- check_budget_alerts -------------------------------------------------


async def test_alert_fires_once_at_80pct_threshold_and_not_again_same_month(db_session):
    user = await _make_user(db_session)
    await upsert_budget(db_session, user.id, "Food", Decimal("1000.00"))
    await _make_expense(db_session, user, amount="850.00", category="Food", txn_date=date(2026, 8, 10))

    alerts = await check_budget_alerts(db_session, user.id, today=date(2026, 8, 15))
    assert len(alerts) == 1
    assert alerts[0].category == "Food"
    assert alerts[0].spent == Decimal("850.00")
    assert alerts[0].monthly_limit == Decimal("1000.00")
    assert alerts[0].days_left == 16  # August has 31 days

    sent_rows = (await db_session.execute(select(BudgetAlertSent))).scalars().all()
    assert len(sent_rows) == 1

    # Running again the same month must not re-fire, even though still >= 80%.
    alerts_again = await check_budget_alerts(db_session, user.id, today=date(2026, 8, 20))
    assert alerts_again == []
    sent_rows_again = (await db_session.execute(select(BudgetAlertSent))).scalars().all()
    assert len(sent_rows_again) == 1  # no duplicate row


async def test_alert_does_not_fire_below_threshold(db_session):
    user = await _make_user(db_session)
    await upsert_budget(db_session, user.id, "Food", Decimal("1000.00"))
    await _make_expense(db_session, user, amount="500.00", category="Food", txn_date=date(2026, 8, 10))

    alerts = await check_budget_alerts(db_session, user.id, today=date(2026, 8, 15))
    assert alerts == []
    assert (await db_session.execute(select(BudgetAlertSent))).scalars().all() == []


async def test_category_with_no_budget_is_silently_skipped(db_session):
    user = await _make_user(db_session)
    # Spend in a category that has no budget row at all.
    await _make_expense(db_session, user, amount="10000.00", category="Shopping", txn_date=date(2026, 8, 10))

    alerts = await check_budget_alerts(db_session, user.id, today=date(2026, 8, 15))
    assert alerts == []


async def test_alert_fires_again_in_a_new_month(db_session):
    """A category that already alerted last month should be free to alert
    again this month — the guard is per (category, month), not permanent."""
    user = await _make_user(db_session)
    await upsert_budget(db_session, user.id, "Food", Decimal("1000.00"))
    await _make_expense(db_session, user, amount="900.00", category="Food", txn_date=date(2026, 7, 10))
    await check_budget_alerts(db_session, user.id, today=date(2026, 7, 15))

    await _make_expense(db_session, user, amount="900.00", category="Food", txn_date=date(2026, 8, 10))
    alerts = await check_budget_alerts(db_session, user.id, today=date(2026, 8, 15))
    assert len(alerts) == 1


async def test_only_current_month_spend_counts_toward_the_limit(db_session):
    user = await _make_user(db_session)
    await upsert_budget(db_session, user.id, "Food", Decimal("1000.00"))
    await _make_expense(db_session, user, amount="900.00", category="Food", txn_date=date(2026, 7, 20))  # last month
    await _make_expense(db_session, user, amount="100.00", category="Food", txn_date=date(2026, 8, 5))  # this month

    alerts = await check_budget_alerts(db_session, user.id, today=date(2026, 8, 15))
    assert alerts == []  # only Rs.100 counts this month — well under 80%


# --- get_budget_statuses / format_budgets_message ------------------------


async def test_get_budget_statuses_lists_limit_and_spend(db_session):
    user = await _make_user(db_session)
    await upsert_budget(db_session, user.id, "Food", Decimal("1000.00"))
    await upsert_budget(db_session, user.id, "Transport", Decimal("500.00"))
    await _make_expense(db_session, user, amount="200.00", category="Food", txn_date=date(2026, 8, 5))

    statuses = await get_budget_statuses(db_session, user.id, today=date(2026, 8, 15))
    by_category = {s.category: s for s in statuses}

    assert by_category["Food"].spent == Decimal("200.00")
    assert by_category["Food"].monthly_limit == Decimal("1000.00")
    assert by_category["Transport"].spent == Decimal("0")


async def test_budget_matching_is_case_insensitive_against_expense_category(db_session):
    """Regression: expenses.category isn't a strict enum — an auto_log
    category from the LLM (or a photo caption's category_hint) isn't
    guaranteed to match /setbudget's exact casing. A "food"-cased expense
    must still count against a "Food"-cased budget."""
    user = await _make_user(db_session)
    await upsert_budget(db_session, user.id, "Food", Decimal("1000.00"))
    await _make_expense(db_session, user, amount="300.00", category="food", txn_date=date(2026, 8, 5))

    statuses = await get_budget_statuses(db_session, user.id, today=date(2026, 8, 15))
    assert statuses[0].spent == Decimal("300.00")

    alerts = await check_budget_alerts(db_session, user.id, today=date(2026, 8, 15))
    assert alerts == []  # 300/1000 = 30%, correctly below the 80% threshold


async def test_format_budgets_message_when_none_set():
    assert "No budgets set" in format_budgets_message([])


# --- message formatting ---------------------------------------------------


def test_budget_alert_message_format():
    alert = BudgetAlert(category="Food", spent=Decimal("3600.00"), monthly_limit=Decimal("4000.00"), days_left=4)
    assert alert.as_message() == "⚠️ Food: Rs.3,600.00 of Rs.4,000.00 budget (90%) — 4 days left this month"


def test_budget_alert_message_singular_day():
    alert = BudgetAlert(category="Food", spent=Decimal("3600.00"), monthly_limit=Decimal("4000.00"), days_left=1)
    assert "1 day left" in alert.as_message()
    assert "1 days left" not in alert.as_message()
