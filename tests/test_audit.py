"""Phase 4: the monthly salary-audit agent (app/audit.py).

Two layers, same split as tests/test_agent.py:
- build_snapshot / _apply_audit_guard: pure functions, tested directly.
- run_monthly_audit: the full loop, driven by a scripted provider against
  aiosqlite, with today pinned so "the previous completed month" is
  deterministic (July 2026 for a today of 2026-08-15).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.audit import _apply_audit_guard, build_snapshot, run_monthly_audit
from app.llm.base import LLMDecisionError
from app.models import AuditRun, Expense, Income, SalaryProfile, User
from app.schemas import AuditReport

TODAY = date(2026, 8, 15)  # → audited month is July 2026
JULY_START = date(2026, 7, 1)
JULY_END = date(2026, 7, 31)


# --- scripted provider -------------------------------------------------------


class _ScriptedAuditProvider:
    def __init__(self, report: AuditReport | None = None, *, error: bool = False) -> None:
        self._report = report
        self._error = error
        self.calls = 0

    async def audit_finances(self, snapshot: dict) -> AuditReport:
        self.calls += 1
        if self._error:
            raise LLMDecisionError("model unavailable")
        assert self._report is not None
        return self._report


def _report(flagged: list[uuid.UUID] | None = None) -> AuditReport:
    return AuditReport(
        summary="Solid month overall.",
        recommendations=["Keep it up."],
        flagged_expense_ids=flagged or [],
        confidence=0.9,
    )


# --- build_snapshot ----------------------------------------------------------


def test_snapshot_totals_and_savings_rate():
    incomes = [Income(amount=Decimal("50000"), txn_date=JULY_START)]
    expenses = [Expense(amount=Decimal("38000"), category="Food", txn_date=date(2026, 7, 5))]
    snap = build_snapshot(JULY_START, JULY_END, incomes, expenses, [], [], None, salary_tolerance_pct=0.05)
    assert snap.period_label == "July 2026"
    assert snap.total_income == Decimal("50000")
    assert snap.total_spend == Decimal("38000")
    assert snap.net_saved == Decimal("12000")
    assert snap.savings_rate == Decimal("24.00")
    assert snap.has_data


def test_snapshot_savings_rate_none_without_income():
    expenses = [Expense(amount=Decimal("100"), category="Food", txn_date=date(2026, 7, 5))]
    snap = build_snapshot(JULY_START, JULY_END, [], expenses, [], [], None, salary_tolerance_pct=0.05)
    assert snap.savings_rate is None
    assert snap.net_saved == Decimal("-100")


def test_snapshot_has_no_data_when_empty():
    snap = build_snapshot(JULY_START, JULY_END, [], [], [], [], None, salary_tolerance_pct=0.05)
    assert not snap.has_data


def test_snapshot_salary_received_within_tolerance():
    profile = SalaryProfile(expected_salary=Decimal("50000"))
    incomes = [Income(amount=Decimal("49500"), txn_date=JULY_START)]  # within 5% of 50000
    snap = build_snapshot(JULY_START, JULY_END, incomes, [], [], [], profile, salary_tolerance_pct=0.05)
    assert snap.salary_received is True
    assert snap.salary_received_amount == Decimal("49500")


def test_snapshot_salary_not_received_when_no_matching_credit():
    profile = SalaryProfile(expected_salary=Decimal("50000"))
    incomes = [Income(amount=Decimal("500"), txn_date=JULY_START)]  # a refund, not the salary
    snap = build_snapshot(JULY_START, JULY_END, incomes, [], [], [], profile, salary_tolerance_pct=0.05)
    assert snap.salary_received is False
    assert snap.salary_received_amount is None


def test_snapshot_salary_received_is_none_without_profile():
    snap = build_snapshot(
        JULY_START,
        JULY_END,
        [Income(amount=Decimal("50000"), txn_date=JULY_START)],
        [],
        [],
        [],
        None,
        salary_tolerance_pct=0.05,
    )
    assert snap.salary_received is None


def test_snapshot_category_breakdown_computes_month_over_month_delta():
    this_month = [Expense(amount=Decimal("300"), category="Food", txn_date=date(2026, 7, 5))]
    prev_month = [Expense(amount=Decimal("200"), category="Food", txn_date=date(2026, 6, 5))]
    snap = build_snapshot(JULY_START, JULY_END, [], this_month, prev_month, [], None, salary_tolerance_pct=0.05)
    food = next(row for row in snap.category_breakdown if row["category"] == "Food")
    assert food["spent"] == "300"
    assert food["prev_spent"] == "200"
    assert food["delta"] == "100"


def test_snapshot_anomaly_candidates_flag_uncategorized_and_outliers():
    # 5+ expenses so the outlier heuristic activates; one Uncategorized, one huge.
    normal = [
        Expense(id=uuid.uuid4(), amount=Decimal("100"), category="Food", txn_date=date(2026, 7, d)) for d in range(1, 6)
    ]
    uncategorized = Expense(id=uuid.uuid4(), amount=Decimal("120"), category="Uncategorized", txn_date=date(2026, 7, 7))
    outlier = Expense(id=uuid.uuid4(), amount=Decimal("9000"), category="Shopping", txn_date=date(2026, 7, 8))
    snap = build_snapshot(
        JULY_START, JULY_END, [], normal + [uncategorized, outlier], [], [], None, salary_tolerance_pct=0.05
    )
    ids = {c["id"] for c in snap.anomaly_candidates}
    assert str(uncategorized.id) in ids
    assert str(outlier.id) in ids
    # A normal Rs.100 Food expense is neither uncategorized nor an outlier.
    assert str(normal[0].id) not in ids


# --- Savings category (money moved to another account, not actually spent) --


def test_snapshot_excludes_savings_from_total_spend():
    expenses = [
        Expense(amount=Decimal("20000"), category="Food", txn_date=date(2026, 7, 5)),
        Expense(amount=Decimal("10000"), category="Savings", txn_date=date(2026, 7, 6)),
    ]
    snap = build_snapshot(
        JULY_START,
        JULY_END,
        [Income(amount=Decimal("50000"), txn_date=JULY_START)],
        expenses,
        [],
        [],
        None,
        salary_tolerance_pct=0.05,
    )
    assert snap.total_spend == Decimal("20000")
    assert snap.moved_to_savings == Decimal("10000")
    # Net saved reflects that the transferred money wasn't spent, not just income - all expenses.
    assert snap.net_saved == Decimal("30000")


def test_snapshot_moved_to_savings_is_case_insensitive():
    expenses = [Expense(amount=Decimal("5000"), category="savings", txn_date=date(2026, 7, 6))]
    snap = build_snapshot(JULY_START, JULY_END, [], expenses, [], [], None, salary_tolerance_pct=0.05)
    assert snap.total_spend == Decimal("0")
    assert snap.moved_to_savings == Decimal("5000")


def test_snapshot_has_data_true_for_savings_only_month():
    expenses = [Expense(amount=Decimal("5000"), category="Savings", txn_date=date(2026, 7, 6))]
    snap = build_snapshot(JULY_START, JULY_END, [], expenses, [], [], None, salary_tolerance_pct=0.05)
    assert snap.has_data


def test_anomaly_candidates_never_flag_savings_as_an_outlier():
    # A large Savings transfer sitting among small everyday expenses must not
    # be flagged as an "unusually large" anomaly — it's deliberate and already
    # correctly categorized.
    normal = [
        Expense(id=uuid.uuid4(), amount=Decimal("100"), category="Food", txn_date=date(2026, 7, d)) for d in range(1, 6)
    ]
    savings_transfer = Expense(id=uuid.uuid4(), amount=Decimal("20000"), category="Savings", txn_date=date(2026, 7, 7))
    snap = build_snapshot(
        JULY_START, JULY_END, [], normal + [savings_transfer], [], [], None, salary_tolerance_pct=0.05
    )
    ids = {c["id"] for c in snap.anomaly_candidates}
    assert str(savings_transfer.id) not in ids


def test_anomaly_outlier_baseline_ignores_savings_amounts():
    # Without excluding Savings from the median, a huge Savings transfer would
    # inflate the baseline and hide a genuinely unusual Food expense.
    normal = [
        Expense(id=uuid.uuid4(), amount=Decimal("100"), category="Food", txn_date=date(2026, 7, d)) for d in range(1, 5)
    ]
    real_outlier = Expense(id=uuid.uuid4(), amount=Decimal("2000"), category="Shopping", txn_date=date(2026, 7, 6))
    savings_transfer = Expense(id=uuid.uuid4(), amount=Decimal("50000"), category="Savings", txn_date=date(2026, 7, 7))
    snap = build_snapshot(
        JULY_START, JULY_END, [], normal + [real_outlier, savings_transfer], [], [], None, salary_tolerance_pct=0.05
    )
    ids = {c["id"] for c in snap.anomaly_candidates}
    assert str(real_outlier.id) in ids


# --- _apply_audit_guard ------------------------------------------------------


def test_guard_drops_ids_not_among_candidates():
    keep, drop = uuid.uuid4(), uuid.uuid4()
    report = _report(flagged=[keep, drop])
    guarded = _apply_audit_guard(report, {str(keep)})
    assert guarded.flagged_expense_ids == [keep]
    # original report is not mutated
    assert set(report.flagged_expense_ids) == {keep, drop}


def test_guard_keeps_all_valid_ids():
    a, b = uuid.uuid4(), uuid.uuid4()
    guarded = _apply_audit_guard(_report(flagged=[a, b]), {str(a), str(b)})
    assert set(guarded.flagged_expense_ids) == {a, b}


# --- run_monthly_audit -------------------------------------------------------


async def _make_user(session, chat_id: int = 5150) -> User:
    user = User(telegram_chat_id=chat_id)
    session.add(user)
    await session.commit()
    return user


async def _seed_july(session, user: User, *, with_uncategorized: bool = False) -> Expense | None:
    session.add(Income(user_id=user.id, amount=Decimal("50000"), source="ACME", txn_date=JULY_START, raw_text="x"))
    session.add(Expense(user_id=user.id, amount=Decimal("2000"), category="Food", txn_date=date(2026, 7, 5)))
    uncategorized: Expense | None = None
    if with_uncategorized:
        uncategorized = Expense(
            user_id=user.id, amount=Decimal("3000"), category="Uncategorized", txn_date=date(2026, 7, 9)
        )
        session.add(uncategorized)
    await session.commit()
    return uncategorized


async def test_run_monthly_audit_completes_and_writes_trail(db_session):
    user = await _make_user(db_session)
    await _seed_july(db_session, user)
    provider = _ScriptedAuditProvider(_report())

    result = await run_monthly_audit(db_session, provider, user.id, today=TODAY)

    assert result.status == "completed"
    assert result.period_label == "July 2026"
    assert result.message is not None
    assert "Salary audit — July 2026" in result.message
    assert "Income: Rs.50,000.00" in result.message
    assert "Spending: Rs.2,000.00" in result.message

    run = (await db_session.execute(select(AuditRun).where(AuditRun.user_id == user.id))).scalar_one()
    assert run.period_month == JULY_START
    assert run.total_income == Decimal("50000")
    assert run.net_saved == Decimal("48000")
    assert run.summary == "Solid month overall."


async def test_run_monthly_audit_flags_anomaly_as_question(db_session):
    user = await _make_user(db_session)
    uncategorized = await _seed_july(db_session, user, with_uncategorized=True)
    assert uncategorized is not None
    provider = _ScriptedAuditProvider(_report(flagged=[uncategorized.id]))

    result = await run_monthly_audit(db_session, provider, user.id, today=TODAY)

    assert result.status == "completed"
    assert len(result.questions) == 1
    assert result.questions[0].expense_id == uncategorized.id
    assert result.questions[0].current_category == "Uncategorized"


async def test_run_monthly_audit_reports_savings_transfer_separately_from_spend(db_session):
    user = await _make_user(db_session)
    await _seed_july(db_session, user)  # 50000 income, 2000 Food
    db_session.add(Expense(user_id=user.id, amount=Decimal("10000"), category="Savings", txn_date=date(2026, 7, 10)))
    await db_session.commit()
    provider = _ScriptedAuditProvider(_report())

    result = await run_monthly_audit(db_session, provider, user.id, today=TODAY)

    assert result.message is not None
    assert "Spending: Rs.2,000.00" in result.message  # savings transfer not counted as spend
    assert "Moved to savings: Rs.10,000.00 (not counted as spend)" in result.message
    assert "Net saved: Rs.48,000.00" in result.message  # 50000 - 2000, not - 12000

    run = (await db_session.execute(select(AuditRun).where(AuditRun.user_id == user.id))).scalar_one()
    assert run.total_spend == Decimal("2000")
    assert run.net_saved == Decimal("48000")


async def test_run_monthly_audit_omits_savings_line_when_none_moved(db_session):
    user = await _make_user(db_session)
    await _seed_july(db_session, user)
    provider = _ScriptedAuditProvider(_report())

    result = await run_monthly_audit(db_session, provider, user.id, today=TODAY)

    assert result.message is not None
    assert "Moved to savings" not in result.message


async def test_run_monthly_audit_guard_drops_hallucinated_flag(db_session):
    user = await _make_user(db_session)
    await _seed_july(db_session, user, with_uncategorized=True)
    # Provider flags an id that isn't among the anomaly candidates offered.
    provider = _ScriptedAuditProvider(_report(flagged=[uuid.uuid4()]))

    result = await run_monthly_audit(db_session, provider, user.id, today=TODAY)

    assert result.status == "completed"
    assert result.questions == []  # guard dropped the bogus id


async def test_run_monthly_audit_is_idempotent(db_session):
    user = await _make_user(db_session)
    await _seed_july(db_session, user)
    provider = _ScriptedAuditProvider(_report())

    first = await run_monthly_audit(db_session, provider, user.id, today=TODAY)
    second = await run_monthly_audit(db_session, provider, user.id, today=TODAY)

    assert first.status == "completed"
    assert second.status == "already_audited"
    assert second.message is not None and "already audited" in second.message
    assert provider.calls == 1  # no second LLM call

    runs = (await db_session.execute(select(AuditRun).where(AuditRun.user_id == user.id))).scalars().all()
    assert len(runs) == 1


async def test_run_monthly_audit_no_data_skips_without_writing(db_session):
    user = await _make_user(db_session)  # nothing seeded
    provider = _ScriptedAuditProvider(_report())

    result = await run_monthly_audit(db_session, provider, user.id, today=TODAY)

    assert result.status == "no_data"
    assert provider.calls == 0
    runs = (await db_session.execute(select(AuditRun).where(AuditRun.user_id == user.id))).scalars().all()
    assert runs == []


async def test_run_monthly_audit_reports_numbers_even_when_llm_fails(db_session):
    user = await _make_user(db_session)
    await _seed_july(db_session, user)
    provider = _ScriptedAuditProvider(error=True)

    result = await run_monthly_audit(db_session, provider, user.id, today=TODAY)

    assert result.status == "completed"
    assert result.message is not None and "Income: Rs.50,000.00" in result.message
    assert "Recommendations:" not in result.message  # no LLM prose
    assert result.questions == []
    run = (await db_session.execute(select(AuditRun).where(AuditRun.user_id == user.id))).scalar_one()
    assert run.summary is None  # numbers persisted, no prose


async def test_run_monthly_audit_reports_salary_received(db_session):
    user = await _make_user(db_session)
    await _seed_july(db_session, user)
    db_session.add(SalaryProfile(user_id=user.id, expected_salary=Decimal("50000"), savings_target=Decimal("10000")))
    await db_session.commit()
    provider = _ScriptedAuditProvider(_report())

    result = await run_monthly_audit(db_session, provider, user.id, today=TODAY)

    assert result.message is not None
    assert "✅ Salary received (Rs.50,000.00)" in result.message
    assert "Savings target" in result.message
