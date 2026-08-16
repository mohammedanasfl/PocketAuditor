"""Stage 3b tests: the constrained query builder. QueryIntent is the only
thing that ever reaches run_query — never the user's raw question, never SQL
text. The injection-safety test below is the concrete version of that claim:
a category value containing SQL metacharacters must be treated as an inert
literal string, not something that can alter the query.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models import Expense, User
from app.query import resolve_date_range, run_query
from app.schemas import QueryIntent


def _intent(**overrides) -> QueryIntent:
    defaults = dict(
        is_expense_question=True,
        category=None,
        date_range="this_week",
        custom_start=None,
        custom_end=None,
        aggregation="sum",
        intent_summary="test",
    )
    defaults.update(overrides)
    return QueryIntent(**defaults)


async def _make_user(session) -> User:
    user = User(telegram_chat_id=13579)
    session.add(user)
    await session.commit()
    return user


async def _make_expense(session, user: User, *, amount: str, category: str, txn_date: date, merchant: str | None = None) -> Expense:
    expense = Expense(user_id=user.id, amount=Decimal(amount), category=category, merchant=merchant, txn_date=txn_date)
    session.add(expense)
    await session.commit()
    return expense


# --- resolve_date_range ---------------------------------------------------


def test_resolve_date_range_today():
    start, end = resolve_date_range(_intent(date_range="today"), today=date(2026, 8, 15))
    assert start == end == date(2026, 8, 15)


def test_resolve_date_range_this_week_monday_start():
    # 2026-08-15 is a Saturday
    start, end = resolve_date_range(_intent(date_range="this_week"), today=date(2026, 8, 15))
    assert start == date(2026, 8, 10)  # Monday
    assert end == date(2026, 8, 15)


def test_resolve_date_range_last_week():
    start, end = resolve_date_range(_intent(date_range="last_week"), today=date(2026, 8, 15))
    assert start == date(2026, 8, 3)
    assert end == date(2026, 8, 9)


def test_resolve_date_range_this_month():
    start, end = resolve_date_range(_intent(date_range="this_month"), today=date(2026, 8, 15))
    assert start == date(2026, 8, 1)
    assert end == date(2026, 8, 15)


def test_resolve_date_range_last_month():
    start, end = resolve_date_range(_intent(date_range="last_month"), today=date(2026, 8, 15))
    assert start == date(2026, 7, 1)
    assert end == date(2026, 7, 31)


def test_resolve_date_range_last_month_across_year_boundary():
    start, end = resolve_date_range(_intent(date_range="last_month"), today=date(2026, 1, 15))
    assert start == date(2025, 12, 1)
    assert end == date(2025, 12, 31)


def test_resolve_date_range_custom():
    intent = _intent(date_range="custom", custom_start=date(2026, 1, 1), custom_end=date(2026, 1, 31))
    start, end = resolve_date_range(intent, today=date(2026, 8, 15))
    assert start == date(2026, 1, 1)
    assert end == date(2026, 1, 31)


def test_resolve_date_range_custom_missing_dates_falls_back_to_this_month():
    intent = _intent(date_range="custom", custom_start=None, custom_end=None)
    start, end = resolve_date_range(intent, today=date(2026, 8, 15))
    assert start == date(2026, 8, 1)
    assert end == date(2026, 8, 15)


# --- run_query: aggregations ------------------------------------------------


async def test_run_query_sum_aggregation(db_session):
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="200.00", category="Food", txn_date=date(2026, 8, 11))
    await _make_expense(db_session, user, amount="300.00", category="Food", txn_date=date(2026, 8, 12))
    await _make_expense(db_session, user, amount="999.00", category="Transport", txn_date=date(2026, 8, 12))

    intent = _intent(category="Food", date_range="this_week", aggregation="sum")
    result = await run_query(db_session, user.id, intent, today=date(2026, 8, 15))

    assert result.total == Decimal("500.00")
    assert result.count == 2
    assert result.as_message() == "You spent Rs.500.00 on Food this week across 2 transactions."


async def test_run_query_count_aggregation(db_session):
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="10.00", category="Food", txn_date=date(2026, 8, 11))
    await _make_expense(db_session, user, amount="10.00", category="Food", txn_date=date(2026, 8, 12))

    intent = _intent(category="Food", date_range="this_week", aggregation="count")
    result = await run_query(db_session, user.id, intent, today=date(2026, 8, 15))
    assert result.count == 2
    assert result.as_message() == "You made 2 transactions on Food this week."


async def test_run_query_max_aggregation(db_session):
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="50.00", category="Food", txn_date=date(2026, 8, 11), merchant="Zomato")
    await _make_expense(db_session, user, amount="450.00", category="Food", txn_date=date(2026, 8, 12), merchant="Blinkit")

    intent = _intent(category="Food", date_range="this_week", aggregation="max")
    result = await run_query(db_session, user.id, intent, today=date(2026, 8, 15))
    assert result.max_item["amount"] == Decimal("450.00")
    assert result.max_item["merchant"] == "Blinkit"
    assert "Rs.450.00" in result.as_message()
    assert "Blinkit" in result.as_message()


async def test_run_query_list_aggregation_caps_at_ten_and_flags_truncation(db_session):
    user = await _make_user(db_session)
    for i in range(12):
        await _make_expense(db_session, user, amount="10.00", category="Food", txn_date=date(2026, 8, 1 + i))

    intent = _intent(category="Food", date_range="this_month", aggregation="list")
    result = await run_query(db_session, user.id, intent, today=date(2026, 8, 15))

    assert result.count == 12
    assert len(result.items) == 10
    assert result.truncated is True
    assert "2 more" in result.as_message()


async def test_run_query_list_aggregation_no_truncation_note_when_under_limit(db_session):
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="10.00", category="Food", txn_date=date(2026, 8, 11))

    intent = _intent(category="Food", date_range="this_week", aggregation="list")
    result = await run_query(db_session, user.id, intent, today=date(2026, 8, 15))
    assert result.truncated is False
    assert "more" not in result.as_message()


async def test_run_query_no_category_covers_all_categories(db_session):
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="100.00", category="Food", txn_date=date(2026, 8, 11))
    await _make_expense(db_session, user, amount="200.00", category="Transport", txn_date=date(2026, 8, 12))

    intent = _intent(category=None, date_range="this_week", aggregation="sum")
    result = await run_query(db_session, user.id, intent, today=date(2026, 8, 15))
    assert result.total == Decimal("300.00")
    assert result.as_message() == "You spent Rs.300.00 this week across 2 transactions."


async def test_run_query_category_match_is_case_insensitive(db_session):
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="100.00", category="Food", txn_date=date(2026, 8, 11))

    intent = _intent(category="food", date_range="this_week", aggregation="sum")  # lowercase
    result = await run_query(db_session, user.id, intent, today=date(2026, 8, 15))
    assert result.total == Decimal("100.00")


async def test_run_query_no_results_message(db_session):
    user = await _make_user(db_session)
    intent = _intent(category="Food", date_range="this_week", aggregation="sum")
    result = await run_query(db_session, user.id, intent, today=date(2026, 8, 15))
    assert result.is_empty
    assert result.as_message() == "No expenses found for that period."


async def test_run_query_only_returns_the_requesting_users_expenses(db_session):
    user = await _make_user(db_session)
    other_user = User(telegram_chat_id=99999)
    db_session.add(other_user)
    await db_session.commit()
    await _make_expense(db_session, other_user, amount="1000.00", category="Food", txn_date=date(2026, 8, 11))

    intent = _intent(category="Food", date_range="this_week", aggregation="sum")
    result = await run_query(db_session, user.id, intent, today=date(2026, 8, 15))
    assert result.is_empty


# --- the actual safety property: no SQL injection via category text -------


async def test_run_query_treats_malicious_category_text_as_an_inert_literal(db_session):
    """The core safety property the brief calls out: whatever text ends up in
    QueryIntent.category, it must never be able to alter the query's
    structure — only ever match (or fail to match) as a plain string."""
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="100.00", category="Food", txn_date=date(2026, 8, 11))
    await _make_expense(db_session, user, amount="200.00", category="Transport", txn_date=date(2026, 8, 12))

    malicious_category = "Food'; DROP TABLE expenses; --"
    intent = _intent(category=malicious_category, date_range="this_week", aggregation="sum")
    result = await run_query(db_session, user.id, intent, today=date(2026, 8, 15))

    # Matches nothing (no category is literally spelled that way) — and,
    # critically, doesn't error or drop any rows for anyone else.
    assert result.is_empty

    everything_intent = _intent(category=None, date_range="this_week", aggregation="list")
    everything = await run_query(db_session, user.id, everything_intent, today=date(2026, 8, 15))
    assert everything.count == 2  # both original rows are still there, untouched
