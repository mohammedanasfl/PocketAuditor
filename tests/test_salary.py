"""Phase 4: the salary-profile service (app/salary.py) — upsert + read.
Pure DB, same shape as tests/test_budgets.py's upsert tests."""

from __future__ import annotations

from decimal import Decimal

from app.models import User
from app.salary import format_salary_profile, get_salary_profile, upsert_salary_profile


async def _make_user(session) -> User:
    user = User(telegram_chat_id=778899)
    session.add(user)
    await session.commit()
    return user


async def test_upsert_creates_profile(db_session):
    user = await _make_user(db_session)

    profile = await upsert_salary_profile(
        db_session, user.id, expected_salary=Decimal("50000"), savings_target=Decimal("10000"), payday_day=1
    )

    assert profile.expected_salary == Decimal("50000")
    assert profile.savings_target == Decimal("10000")
    assert profile.payday_day == 1

    fetched = await get_salary_profile(db_session, user.id)
    assert fetched is not None
    assert fetched.id == profile.id


async def test_upsert_updates_existing_profile(db_session):
    user = await _make_user(db_session)
    await upsert_salary_profile(db_session, user.id, expected_salary=Decimal("50000"))
    await upsert_salary_profile(
        db_session, user.id, expected_salary=Decimal("60000"), savings_target=Decimal("15000"), payday_day=5
    )

    profile = await get_salary_profile(db_session, user.id)
    assert profile is not None
    assert profile.expected_salary == Decimal("60000")
    assert profile.savings_target == Decimal("15000")
    assert profile.payday_day == 5

    # Still one row, not two.
    from sqlalchemy import func, select

    from app.models import SalaryProfile

    count = (
        await db_session.execute(
            select(func.count()).select_from(SalaryProfile).where(SalaryProfile.user_id == user.id)
        )
    ).scalar_one()
    assert count == 1


async def test_get_profile_none_for_new_user(db_session):
    user = await _make_user(db_session)
    assert await get_salary_profile(db_session, user.id) is None


def test_format_profile_none():
    assert format_salary_profile(None) == "No salary profile set yet."


async def test_format_profile_includes_all_set_fields(db_session):
    user = await _make_user(db_session)
    profile = await upsert_salary_profile(
        db_session, user.id, expected_salary=Decimal("50000"), savings_target=Decimal("10000"), payday_day=1
    )
    text = format_salary_profile(profile)
    assert "Rs.50,000.00" in text
    assert "Rs.10,000.00" in text
    assert "day 1" in text


async def test_format_profile_omits_unset_optional_fields(db_session):
    user = await _make_user(db_session)
    profile = await upsert_salary_profile(db_session, user.id, expected_salary=Decimal("50000"))
    text = format_salary_profile(profile)
    assert "Rs.50,000.00" in text
    assert "Savings target" not in text
    assert "Payday" not in text
