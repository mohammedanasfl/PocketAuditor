"""app.digest — the deterministic weekly summary (no LLM). Uses a fixed
`today` throughout so tests don't depend on which real-world day they run."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.digest import build_weekly_digest
from app.models import Expense, User

_TODAY = date(2026, 8, 12)  # a Wednesday
_WEEK_START = _TODAY - timedelta(days=_TODAY.weekday())


async def _make_user(session) -> User:
    user = User(telegram_chat_id=13579)
    session.add(user)
    await session.commit()
    return user


async def _make_expense(
    session, user: User, *, amount: str, category: str, txn_date: date, merchant: str | None = None
) -> Expense:
    expense = Expense(user_id=user.id, amount=Decimal(amount), category=category, txn_date=txn_date, merchant=merchant)
    session.add(expense)
    await session.commit()
    return expense


async def test_digest_reuses_spend_summary_totals(db_session):
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="100.00", category="Food", txn_date=_TODAY)
    await _make_expense(db_session, user, amount="50.00", category="Food", txn_date=_WEEK_START - timedelta(days=1))

    digest = await build_weekly_digest(db_session, user.id, today=_TODAY)

    assert digest.week_spend == Decimal("100.00")
    assert digest.month_spend == Decimal("150.00")


async def test_digest_top_category_this_week(db_session):
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="100.00", category="Food", txn_date=_TODAY)
    await _make_expense(db_session, user, amount="300.00", category="Shopping", txn_date=_TODAY)
    # Outside the current week — must not count toward "this week"'s top category
    await _make_expense(db_session, user, amount="900.00", category="Bills", txn_date=_WEEK_START - timedelta(days=1))

    digest = await build_weekly_digest(db_session, user.id, today=_TODAY)

    assert digest.top_category == ("Shopping", Decimal("300.00"))


async def test_digest_biggest_expense_this_week(db_session):
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="100.00", category="Food", txn_date=_TODAY, merchant="Zomato")
    await _make_expense(db_session, user, amount="900.00", category="Shopping", txn_date=_TODAY, merchant="Amazon")

    digest = await build_weekly_digest(db_session, user.id, today=_TODAY)

    assert digest.biggest_expense == {"amount": Decimal("900.00"), "merchant": "Amazon", "category": "Shopping"}


async def test_digest_excludes_savings_category(db_session):
    """Consistent with /spend, the monthly audit, and /ask's net aggregation —
    the fifth call site applying the same Savings exclusion."""
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="100.00", category="Food", txn_date=_TODAY)
    await _make_expense(db_session, user, amount="5000.00", category="Savings", txn_date=_TODAY, merchant="My Bank")

    digest = await build_weekly_digest(db_session, user.id, today=_TODAY)

    assert digest.top_category == ("Food", Decimal("100.00"))
    assert digest.biggest_expense["category"] == "Food"
    assert digest.week_spend == Decimal("100.00")


async def test_digest_handles_zero_activity(db_session):
    user = await _make_user(db_session)

    digest = await build_weekly_digest(db_session, user.id, today=_TODAY)

    assert digest.week_spend == Decimal("0")
    assert digest.top_category is None
    assert digest.biggest_expense is None
    message = digest.as_message()  # must not crash on None fields
    assert "This week: Rs.0.00" in message
    assert "Top category" not in message
    assert "Biggest expense" not in message


async def test_digest_as_message_includes_top_category_and_biggest_expense(db_session):
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="900.00", category="Shopping", txn_date=_TODAY, merchant="Amazon")

    digest = await build_weekly_digest(db_session, user.id, today=_TODAY)
    message = digest.as_message()

    assert "Top category this week: Shopping (Rs.900.00)" in message
    assert "Biggest expense this week: Rs.900.00 at Amazon (Shopping)" in message


async def test_digest_biggest_expense_tie_is_deterministic(db_session):
    """Two expenses with the identical max amount must not make the query
    non-deterministic — some tie-break is required."""
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="500.00", category="Food", txn_date=_TODAY, merchant="A")
    await _make_expense(db_session, user, amount="500.00", category="Shopping", txn_date=_TODAY, merchant="B")

    digest_1 = await build_weekly_digest(db_session, user.id, today=_TODAY)
    digest_2 = await build_weekly_digest(db_session, user.id, today=_TODAY)

    assert digest_1.biggest_expense == digest_2.biggest_expense
