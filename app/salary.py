"""Salary-profile service: the per-user expected-salary settings the monthly
audit and the mid-month alerts compare actual income/spend against.

Pure DB, no LLM — same shape as app/budgets.py's upsert_budget/get_* helpers.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SalaryProfile

logger = logging.getLogger(__name__)


async def upsert_salary_profile(
    session: AsyncSession,
    user_id: UUID,
    *,
    expected_salary: Decimal,
    savings_target: Decimal | None = None,
    payday_day: int | None = None,
) -> SalaryProfile:
    """Create or update the single (user_id) salary-profile row."""
    profile = (
        await session.execute(select(SalaryProfile).where(SalaryProfile.user_id == user_id))
    ).scalar_one_or_none()
    if profile is None:
        profile = SalaryProfile(
            user_id=user_id,
            expected_salary=expected_salary,
            savings_target=savings_target,
            payday_day=payday_day,
        )
        session.add(profile)
    else:
        profile.expected_salary = expected_salary
        profile.savings_target = savings_target
        profile.payday_day = payday_day
    await session.commit()
    logger.info(
        "upserted salary profile user=%s expected=%s savings_target=%s payday=%s",
        user_id,
        expected_salary,
        savings_target,
        payday_day,
    )
    return profile


async def get_salary_profile(session: AsyncSession, user_id: UUID) -> SalaryProfile | None:
    return (await session.execute(select(SalaryProfile).where(SalaryProfile.user_id == user_id))).scalar_one_or_none()


def format_salary_profile(profile: SalaryProfile | None) -> str:
    if profile is None:
        return "No salary profile set yet."
    lines = [f"💼 Salary profile:\nExpected: Rs.{profile.expected_salary:,.2f}/month"]
    if profile.savings_target is not None:
        lines.append(f"Savings target: Rs.{profile.savings_target:,.2f}/month")
    if profile.payday_day is not None:
        lines.append(f"Payday: day {profile.payday_day} of the month")
    return "\n".join(lines)
