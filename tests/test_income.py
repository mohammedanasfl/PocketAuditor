"""Phase 4: the income ledger service (app/income.py) — recording a money-in
row and summarizing income by period. Same conventions as tests/test_reports.py
and tests/test_budgets.py: an injectable `today` pins the clock so the
period-bucketing math is deterministic rather than dependent on the wall clock.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.income import IncomeSummary, get_income_summary, record_income
from app.models import Income, User


async def _make_user(session, chat_id: int = 13579) -> User:
    user = User(telegram_chat_id=chat_id)
    session.add(user)
    await session.commit()
    return user


async def test_record_income_writes_a_row(db_session):
    user = await _make_user(db_session)

    income = await record_income(
        db_session,
        user.id,
        amount=Decimal("50000.00"),
        source="EMPLOYER PVT LTD",
        txn_date=date(2026, 8, 1),
        raw_text="INR 50,000 credited ... from EMPLOYER PVT LTD",
    )

    stored = (await db_session.execute(select(Income).where(Income.user_id == user.id))).scalar_one()
    assert stored.id == income.id
    assert stored.amount == Decimal("50000.00")
    assert stored.source == "EMPLOYER PVT LTD"
    assert stored.created_via == "auto"  # default


async def test_income_summary_buckets_by_period(db_session):
    user = await _make_user(db_session)
    today = date(2026, 8, 17)

    async def _income(amount: str, txn_date: date) -> None:
        await record_income(db_session, user.id, amount=Decimal(amount), source="X", txn_date=txn_date, raw_text="x")

    await _income("50000.00", date(2026, 8, 1))  # this month
    await _income("2000.00", date(2026, 7, 20))  # last month (July) + this year
    await _income("500.00", date(2026, 1, 5))  # earlier this year only
    await _income("999.00", date(2025, 12, 31))  # last year → all-time only

    summary = await get_income_summary(db_session, user.id, today=today)

    assert summary.month == Decimal("50000.00")
    assert summary.last_month == Decimal("2000.00")
    assert summary.year == Decimal("52500.00")  # 50000 + 2000 + 500
    assert summary.total == Decimal("53499.00")


async def test_income_summary_zero_when_no_income(db_session):
    user = await _make_user(db_session)
    summary = await get_income_summary(db_session, user.id, today=date(2026, 8, 17))
    assert summary.month == summary.last_month == summary.year == summary.total == Decimal("0")


async def test_income_summary_scopes_to_the_requesting_user(db_session):
    user_a = await _make_user(db_session, chat_id=111)
    user_b = await _make_user(db_session, chat_id=222)

    await record_income(
        db_session, user_a.id, amount=Decimal("100"), source=None, txn_date=date(2026, 8, 1), raw_text="a"
    )
    await record_income(
        db_session, user_b.id, amount=Decimal("900"), source=None, txn_date=date(2026, 8, 1), raw_text="b"
    )

    summary = await get_income_summary(db_session, user_a.id, today=date(2026, 8, 17))
    assert summary.total == Decimal("100")


def test_income_summary_as_message_formats_all_periods():
    summary = IncomeSummary(
        month=Decimal("50000.00"), last_month=Decimal("48000.00"), year=Decimal("400000.00"), total=Decimal("999999.99")
    )
    text = summary.as_message()
    assert "💵 Income summary" in text
    assert "This month: Rs.50,000.00" in text
    assert "Last month: Rs.48,000.00" in text
    assert "This year: Rs.400,000.00" in text
    assert "All time: Rs.999,999.99" in text
