"""Phase 4: proactive mid-month alerts (app/audit.py:check_midmonth_alerts).
Deterministic, no LLM. Same testing shape as tests/test_budgets.py — an
injectable `today` pins the clock, and a dedup table stops re-firing.

`today` is mid-August 2026 (day 20) so the month-to-date window and pace
projection are meaningful.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.audit import MidMonthAlert, check_midmonth_alerts
from app.models import AuditAlertSent, Expense, Income, SalaryProfile, User

TODAY = date(2026, 8, 20)
AUG = date(2026, 8, 1)


async def _make_user(session, chat_id: int = 2024) -> User:
    user = User(telegram_chat_id=chat_id)
    session.add(user)
    await session.commit()
    return user


async def _profile(session, user, *, expected="50000", savings=None, payday=None) -> None:
    session.add(
        SalaryProfile(
            user_id=user.id,
            expected_salary=Decimal(expected),
            savings_target=Decimal(savings) if savings is not None else None,
            payday_day=payday,
        )
    )
    await session.commit()


async def _spend(session, user, amount: str, day: int = 5) -> None:
    session.add(Expense(user_id=user.id, amount=Decimal(amount), category="Food", txn_date=date(2026, 8, day)))
    await session.commit()


async def test_no_profile_no_alerts(db_session):
    user = await _make_user(db_session)
    assert await check_midmonth_alerts(db_session, user.id, today=TODAY) == []


async def test_salary_late_fires_when_payday_passed_and_no_matching_credit(db_session):
    user = await _make_user(db_session)
    await _profile(db_session, user, expected="50000", payday=1)  # due by day 3; today is day 20

    alerts = await check_midmonth_alerts(db_session, user.id, today=TODAY)

    assert [a.alert_type for a in alerts] == ["salary_late"]
    assert "hasn't arrived" in alerts[0].as_message()


async def test_salary_late_does_not_fire_when_salary_received(db_session):
    user = await _make_user(db_session)
    await _profile(db_session, user, expected="50000", payday=1)
    db_session.add(Income(user_id=user.id, amount=Decimal("50000"), source="ACME", txn_date=AUG, raw_text="x"))
    await db_session.commit()

    assert await check_midmonth_alerts(db_session, user.id, today=TODAY) == []


async def test_salary_late_does_not_fire_before_grace_period(db_session):
    user = await _make_user(db_session)
    await _profile(db_session, user, expected="50000", payday=1)  # due by day 3

    # today is day 2 — still within payday + grace, so not "late" yet.
    assert await check_midmonth_alerts(db_session, user.id, today=date(2026, 8, 2)) == []


async def test_pace_high_fires_when_projected_spend_exceeds_ceiling(db_session):
    user = await _make_user(db_session)
    await _profile(db_session, user, expected="50000", savings="10000")  # ceiling 40000
    await _spend(db_session, user, "30000")  # projected = 30000/20*31 = 46,500 > 40,000

    alerts = await check_midmonth_alerts(db_session, user.id, today=TODAY)

    assert [a.alert_type for a in alerts] == ["pace_high"]
    assert "on track to spend" in alerts[0].as_message()


async def test_pace_high_does_not_fire_when_on_track(db_session):
    user = await _make_user(db_session)
    await _profile(db_session, user, expected="50000", savings="10000")
    await _spend(db_session, user, "10000")  # projected = 15,500 < 40,000

    assert await check_midmonth_alerts(db_session, user.id, today=TODAY) == []


async def test_pace_high_suppressed_too_early_in_month(db_session):
    user = await _make_user(db_session)
    await _profile(db_session, user, expected="50000", savings="10000")
    await _spend(db_session, user, "30000", day=1)

    # Day 3 is before the minimum window for a trustworthy projection.
    assert await check_midmonth_alerts(db_session, user.id, today=date(2026, 8, 3)) == []


async def test_alerts_fire_at_most_once_per_month(db_session):
    user = await _make_user(db_session)
    await _profile(db_session, user, expected="50000", payday=1)

    first = await check_midmonth_alerts(db_session, user.id, today=TODAY)
    second = await check_midmonth_alerts(db_session, user.id, today=TODAY)

    assert [a.alert_type for a in first] == ["salary_late"]
    assert second == []  # deduped

    rows = (await db_session.execute(select(AuditAlertSent).where(AuditAlertSent.user_id == user.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].alert_type == "salary_late"
    assert rows[0].month == AUG


def test_midmonth_alert_as_message():
    alert = MidMonthAlert(alert_type="salary_late", message="⚠️ heads up")
    assert alert.as_message() == "⚠️ heads up"
