"""The monthly salary-audit agent: perceive -> compute -> decide -> act,
structured exactly like app/agent.py's reconcile loop but running once a month
over a whole month of income and spending rather than per-transaction.

Load-bearing design rule (same as app/query.py and app/agent.py's _apply_guard):
every exact figure — income, spend, savings rate, category totals — is computed
here in code and shown to the user verbatim. The LLM is the auditor's *voice*,
not its calculator: it receives the pre-computed snapshot and returns only prose
(summary + recommendations) plus which of the anomaly candidates *we* selected
to flag. _apply_audit_guard then re-checks those ids against the offered set in
code, discarding any the model invented — the same hallucination guard
_apply_guard applies to a match's expense id.

Imports only app.llm.base + app.schemas (LLM side) and app.models (DB side) —
never a concrete provider — so the provider swap and the scripted-FakeProvider
tests stay cheap, just like app/agent.py.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.llm.base import LLMDecisionError, LLMProvider
from app.models import AuditAlertSent, AuditRun, Budget, Expense, Income, SalaryProfile
from app.schemas import AuditReport

logger = logging.getLogger(__name__)

_MAX_ANOMALY_CANDIDATES = 8
# An expense counts as a "large" anomaly only once there are enough expenses
# for a median to mean anything, and only when it clears this multiple of it.
_OUTLIER_MULTIPLE = Decimal("3")
_MIN_EXPENSES_FOR_OUTLIER = 5
_UNCATEGORIZED = "Uncategorized"
# Don't project a spending pace from too few days — one big day-2 purchase
# shouldn't burn the month's single pace alert on a false alarm.
_MIN_DAY_FOR_PACE_ALERT = 7


@dataclass
class AuditQuestion:
    """One anomaly the audit wants the user to categorize — the Phase 4 analog
    of app/agent.py's PendingQuestion. Telegram concerns stay out of this
    module; the caller turns these into inline-button messages."""

    expense_id: UUID
    amount: Decimal
    merchant: str | None
    current_category: str
    reason: str


@dataclass
class FinancialSnapshot:
    """The deterministic, pre-computed facts for one month. Everything the
    monthly report shows and everything the LLM is allowed to reason over."""

    period_month: date
    period_label: str
    total_income: Decimal
    total_spend: Decimal
    net_saved: Decimal
    savings_rate: Decimal | None
    expected_salary: Decimal | None
    savings_target: Decimal | None
    salary_received: bool | None
    salary_received_amount: Decimal | None
    category_breakdown: list[dict]
    budgets: list[dict]
    anomaly_candidates: list[dict]

    @property
    def has_data(self) -> bool:
        return self.total_income != 0 or self.total_spend != 0

    def to_llm_dict(self) -> dict:
        """Snapshot as handed to the LLM. Ids are stringified so the model
        echoes strings that match _apply_audit_guard's candidate id set."""
        return {
            "period": self.period_label,
            "total_income": str(self.total_income),
            "total_spend": str(self.total_spend),
            "net_saved": str(self.net_saved),
            "savings_rate_pct": None if self.savings_rate is None else str(self.savings_rate),
            "expected_salary": None if self.expected_salary is None else str(self.expected_salary),
            "savings_target": None if self.savings_target is None else str(self.savings_target),
            "salary_received": self.salary_received,
            "salary_received_amount": (
                None if self.salary_received_amount is None else str(self.salary_received_amount)
            ),
            "category_breakdown": self.category_breakdown,
            "budgets": self.budgets,
            "anomaly_candidates": self.anomaly_candidates,
        }


@dataclass
class MonthlyAuditResult:
    status: Literal["completed", "already_audited", "no_data"]
    period_month: date
    period_label: str
    message: str | None = None
    questions: list[AuditQuestion] = field(default_factory=list)


def _last_completed_month(today: date) -> tuple[date, date]:
    """(first, last) day of the calendar month before `today`'s month. Same
    arithmetic as app/query.py's resolve_date_range 'last_month' branch."""
    this_month_start = today.replace(day=1)
    last_month_end = this_month_start - timedelta(days=1)
    return last_month_end.replace(day=1), last_month_end


def _median(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _group_by_category(expenses: list[Expense]) -> dict[str, Decimal]:
    """Keyed by lowercased category, same case-insensitive treatment as
    app/budgets.py — expenses.category isn't a strict enum."""
    out: dict[str, Decimal] = {}
    for expense in expenses:
        key = expense.category.lower()
        out[key] = out.get(key, Decimal("0")) + expense.amount
    return out


def _find_salary_credit(incomes: list[Income], expected: Decimal | None, tolerance_pct: float) -> Decimal | None:
    """The credit closest to the expected salary within tolerance, or None if
    no credit matches (or no expected figure is set). Shared by the monthly
    audit's 'salary received' fact and the mid-month 'salary late' check so
    both judge 'did the salary arrive' the exact same way."""
    if expected is None or expected <= 0:
        return None
    tolerance = expected * Decimal(str(tolerance_pct))
    matches = [i.amount for i in incomes if abs(i.amount - expected) <= tolerance]
    return min(matches, key=lambda amt: abs(amt - expected)) if matches else None


async def _load_month(
    session: AsyncSession, user_id: UUID, month_start: date, month_end: date, prev_start: date, prev_end: date
) -> tuple[list[Income], list[Expense], list[Expense], list[Budget], SalaryProfile | None]:
    incomes = (
        (
            await session.execute(
                select(Income).where(
                    Income.user_id == user_id,
                    Income.txn_date >= month_start,
                    Income.txn_date <= month_end,
                )
            )
        )
        .scalars()
        .all()
    )
    expenses = (
        (
            await session.execute(
                select(Expense).where(
                    Expense.user_id == user_id,
                    Expense.txn_date >= month_start,
                    Expense.txn_date <= month_end,
                )
            )
        )
        .scalars()
        .all()
    )
    prev_expenses = (
        (
            await session.execute(
                select(Expense).where(
                    Expense.user_id == user_id,
                    Expense.txn_date >= prev_start,
                    Expense.txn_date <= prev_end,
                )
            )
        )
        .scalars()
        .all()
    )
    budgets = (await session.execute(select(Budget).where(Budget.user_id == user_id))).scalars().all()
    profile = (
        await session.execute(select(SalaryProfile).where(SalaryProfile.user_id == user_id))
    ).scalar_one_or_none()
    return list(incomes), list(expenses), list(prev_expenses), list(budgets), profile


def _select_anomaly_candidates(expenses: list[Expense]) -> list[dict]:
    """Expenses worth asking the user to review: anything left Uncategorized,
    plus statistical outliers (only when there are enough expenses for a median
    to be meaningful). Capped and sorted largest-first."""
    if not expenses:
        return []

    median = _median([e.amount for e in expenses])
    outlier_floor = median * _OUTLIER_MULTIPLE
    enough_for_outliers = len(expenses) >= _MIN_EXPENSES_FOR_OUTLIER

    picked: list[tuple[Expense, str]] = []
    for expense in expenses:
        if expense.category.lower() == _UNCATEGORIZED.lower():
            picked.append((expense, "left uncategorized"))
        elif enough_for_outliers and median > 0 and expense.amount >= outlier_floor:
            picked.append((expense, "unusually large versus your typical spend this month"))

    picked.sort(key=lambda pair: pair[0].amount, reverse=True)
    return [
        {
            "id": str(expense.id),
            "amount": str(expense.amount),
            "merchant": expense.merchant,
            "category": expense.category,
            "txn_date": expense.txn_date.isoformat(),
            "reason": reason,
        }
        for expense, reason in picked[:_MAX_ANOMALY_CANDIDATES]
    ]


def build_snapshot(
    period_month: date,
    period_end: date,
    incomes: list[Income],
    expenses: list[Expense],
    prev_expenses: list[Expense],
    budgets: list[Budget],
    profile: SalaryProfile | None,
    *,
    salary_tolerance_pct: float,
) -> FinancialSnapshot:
    """Pure function (no I/O): turn a month's raw rows into the computed facts.
    Kept separate from _load_month so tests can drive it directly."""
    total_income = sum((i.amount for i in incomes), Decimal("0"))
    total_spend = sum((e.amount for e in expenses), Decimal("0"))
    net_saved = total_income - total_spend
    savings_rate = (net_saved / total_income * 100).quantize(Decimal("0.01")) if total_income > 0 else None

    this_by_cat = _group_by_category(expenses)
    prev_by_cat = _group_by_category(prev_expenses)
    # Preserve a display label (the first-seen original casing) per lowered key.
    labels: dict[str, str] = {}
    for expense in expenses + prev_expenses:
        labels.setdefault(expense.category.lower(), expense.category)
    category_breakdown = [
        {
            "category": labels[key],
            "spent": str(this_by_cat.get(key, Decimal("0"))),
            "prev_spent": str(prev_by_cat.get(key, Decimal("0"))),
            "delta": str(this_by_cat.get(key, Decimal("0")) - prev_by_cat.get(key, Decimal("0"))),
        }
        for key in sorted(set(this_by_cat) | set(prev_by_cat))
    ]

    expected_salary = profile.expected_salary if profile else None
    savings_target = profile.savings_target if profile else None
    salary_received: bool | None = None
    salary_received_amount: Decimal | None = None
    if expected_salary is not None and expected_salary > 0:
        salary_received_amount = _find_salary_credit(incomes, expected_salary, salary_tolerance_pct)
        salary_received = salary_received_amount is not None

    return FinancialSnapshot(
        period_month=period_month,
        period_label=f"{calendar.month_name[period_month.month]} {period_month.year}",
        total_income=total_income,
        total_spend=total_spend,
        net_saved=net_saved,
        savings_rate=savings_rate,
        expected_salary=expected_salary,
        savings_target=savings_target,
        salary_received=salary_received,
        salary_received_amount=salary_received_amount,
        category_breakdown=category_breakdown,
        budgets=[{"category": b.category, "monthly_limit": str(b.monthly_limit)} for b in budgets],
        anomaly_candidates=_select_anomaly_candidates(expenses),
    )


def _apply_audit_guard(report: AuditReport, candidate_ids: set[str]) -> AuditReport:
    """Drop any flagged expense id the model named that wasn't among the
    anomaly candidates we offered — a hallucinated id, the same failure
    app/agent.py's _apply_guard rejects for matched_expense_id. Returns a new
    AuditReport so the original model output is never silently mutated."""
    kept = [eid for eid in report.flagged_expense_ids if str(eid) in candidate_ids]
    dropped = len(report.flagged_expense_ids) - len(kept)
    if dropped:
        logger.info("audit guard fired: dropped %d flagged id(s) not among the candidates offered", dropped)
    return AuditReport(
        summary=report.summary,
        recommendations=report.recommendations,
        flagged_expense_ids=kept,
        confidence=report.confidence,
    )


def _format_message(snapshot: FinancialSnapshot, report: AuditReport | None) -> str:
    """The report the user sees: numbers computed here, prose (if any) from the
    LLM. When the LLM call failed, the numbers still go out on their own."""
    lines = [f"🔍 Salary audit — {snapshot.period_label}"]
    lines.append(f"Income: Rs.{snapshot.total_income:,.2f}")
    lines.append(f"Spending: Rs.{snapshot.total_spend:,.2f}")
    rate = "" if snapshot.savings_rate is None else f" ({snapshot.savings_rate:.0f}% of income)"
    verb = "saved" if snapshot.net_saved >= 0 else "overspent by"
    amount = snapshot.net_saved if snapshot.net_saved >= 0 else -snapshot.net_saved
    lines.append(f"Net {verb}: Rs.{amount:,.2f}{rate}")

    if snapshot.expected_salary is not None:
        if snapshot.salary_received:
            lines.append(f"✅ Salary received (Rs.{snapshot.salary_received_amount:,.2f})")
        else:
            lines.append(f"⚠️ Expected salary of Rs.{snapshot.expected_salary:,.2f} not detected this month")
        if snapshot.savings_target is not None:
            hit = snapshot.net_saved >= snapshot.savings_target
            mark = "✅" if hit else "❌"
            lines.append(f"{mark} Savings target Rs.{snapshot.savings_target:,.2f} — {'met' if hit else 'missed'}")

    if report is not None and report.summary:
        lines.append("")
        lines.append(report.summary)
    if report is not None and report.recommendations:
        lines.append("")
        lines.append("Recommendations:")
        lines.extend(f"• {rec}" for rec in report.recommendations)

    return "\n".join(lines)


def _recap_message(run: AuditRun) -> str:
    label = f"{calendar.month_name[run.period_month.month]} {run.period_month.year}"
    return (
        f"🔍 {label} was already audited.\n"
        f"Income: Rs.{run.total_income:,.2f}, Spending: Rs.{run.total_spend:,.2f}, "
        f"Net saved: Rs.{run.net_saved:,.2f}."
    )


async def run_monthly_audit(
    session: AsyncSession, provider: LLMProvider, user_id: UUID, *, today: date | None = None
) -> MonthlyAuditResult:
    """Audit the previous completed month for one user. Idempotent: the
    (user_id, period_month) row in audit_runs is both the trail and the
    once-per-month guard, so a re-run returns 'already_audited' without a
    second LLM call — same insert-guards-the-send shape as check_budget_alerts."""
    today = today or date.today()
    month_start, month_end = _last_completed_month(today)
    prev_start, prev_end = _last_completed_month(month_start)
    label = f"{calendar.month_name[month_start.month]} {month_start.year}"

    existing = (
        await session.execute(select(AuditRun).where(AuditRun.user_id == user_id, AuditRun.period_month == month_start))
    ).scalar_one_or_none()
    if existing is not None:
        logger.info("run_monthly_audit: user=%s %s already audited, skipping", user_id, label)
        return MonthlyAuditResult(
            status="already_audited", period_month=month_start, period_label=label, message=_recap_message(existing)
        )

    incomes, expenses, prev_expenses, budgets, profile = await _load_month(
        session, user_id, month_start, month_end, prev_start, prev_end
    )
    snapshot = build_snapshot(
        month_start,
        month_end,
        incomes,
        expenses,
        prev_expenses,
        budgets,
        profile,
        salary_tolerance_pct=settings.salary_match_tolerance_pct,
    )

    if not snapshot.has_data:
        logger.info("run_monthly_audit: user=%s %s has no income or spend, skipping", user_id, label)
        return MonthlyAuditResult(status="no_data", period_month=month_start, period_label=label)

    # The LLM only supplies prose; if it fails, the numbers still go out. Same
    # resilience as app/parser.py falling back to the regex result.
    report: AuditReport | None = None
    try:
        raw_report = await provider.audit_finances(snapshot.to_llm_dict())
        candidate_ids = {c["id"] for c in snapshot.anomaly_candidates}
        report = _apply_audit_guard(raw_report, candidate_ids)
    except LLMDecisionError as exc:
        logger.warning("run_monthly_audit: user=%s audit_finances failed, sending numbers only (%s)", user_id, exc)

    questions: list[AuditQuestion] = []
    if report is not None:
        by_id = {c["id"]: c for c in snapshot.anomaly_candidates}
        for flagged in report.flagged_expense_ids:
            candidate = by_id[str(flagged)]  # guard guarantees membership
            questions.append(
                AuditQuestion(
                    expense_id=flagged,
                    amount=Decimal(candidate["amount"]),
                    merchant=candidate["merchant"],
                    current_category=candidate["category"],
                    reason=candidate["reason"],
                )
            )

    run = AuditRun(
        user_id=user_id,
        period_month=month_start,
        total_income=snapshot.total_income,
        total_spend=snapshot.total_spend,
        net_saved=snapshot.net_saved,
        savings_rate=snapshot.savings_rate,
        summary=report.summary if report is not None else None,
    )
    session.add(run)
    await session.commit()

    message = _format_message(snapshot, report)
    logger.info(
        "run_monthly_audit: user=%s %s done — income=%s spend=%s saved=%s questions=%d",
        user_id,
        label,
        snapshot.total_income,
        snapshot.total_spend,
        snapshot.net_saved,
        len(questions),
    )
    return MonthlyAuditResult(
        status="completed", period_month=month_start, period_label=label, message=message, questions=questions
    )


@dataclass
class MidMonthAlert:
    """A proactive within-the-month warning (Phase 4). Deterministic, no LLM —
    same "numbers computed in code" rule as app/budgets.py's BudgetAlert."""

    alert_type: str  # 'salary_late' | 'pace_high'
    message: str

    def as_message(self) -> str:
        return self.message


async def check_midmonth_alerts(
    session: AsyncSession, user_id: UUID, *, today: date | None = None
) -> list[MidMonthAlert]:
    """Proactive mid-month checks against the user's salary profile, fired at
    most once each per calendar month (dedup via audit_alerts_sent, the same
    insert-before-send guard check_budget_alerts uses):

    - salary_late: payday + grace has passed and no credit matching the
      expected salary has arrived this month.
    - pace_high: month-to-date spend, projected to month-end, exceeds the
      income-minus-savings-target ceiling.

    Returns [] when the user has no salary profile — there's nothing to
    compare actual income/spend against, so no alert can be grounded.
    """
    today = today or date.today()
    month_start = today.replace(day=1)

    profile = (
        await session.execute(select(SalaryProfile).where(SalaryProfile.user_id == user_id))
    ).scalar_one_or_none()
    if profile is None:
        return []

    incomes = (
        (
            await session.execute(
                select(Income).where(
                    Income.user_id == user_id, Income.txn_date >= month_start, Income.txn_date <= today
                )
            )
        )
        .scalars()
        .all()
    )
    mtd_spend = Decimal(
        str(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Expense.amount), 0)).where(
                        Expense.user_id == user_id, Expense.txn_date >= month_start, Expense.txn_date <= today
                    )
                )
            ).scalar_one()
        )
    )
    days_in_month = calendar.monthrange(today.year, today.month)[1]

    candidates: list[MidMonthAlert] = []

    if profile.payday_day is not None and profile.expected_salary and profile.expected_salary > 0:
        due_by = profile.payday_day + settings.midmonth_alert_grace_days
        received = _find_salary_credit(list(incomes), profile.expected_salary, settings.salary_match_tolerance_pct)
        if today.day > due_by and received is None:
            candidates.append(
                MidMonthAlert(
                    alert_type="salary_late",
                    message=(
                        f"⚠️ Your expected salary of Rs.{profile.expected_salary:,.2f} hasn't arrived yet — "
                        f"it was due around day {profile.payday_day} and it's now day {today.day}."
                    ),
                )
            )

    if profile.savings_target is not None and profile.expected_salary and today.day >= _MIN_DAY_FOR_PACE_ALERT:
        ceiling = profile.expected_salary - profile.savings_target
        projected = mtd_spend / today.day * days_in_month
        if ceiling >= 0 and projected > ceiling:
            candidates.append(
                MidMonthAlert(
                    alert_type="pace_high",
                    message=(
                        f"📈 At your current pace you're on track to spend about Rs.{projected:,.2f} this month, "
                        f"over your Rs.{ceiling:,.2f} ceiling (expected income minus your savings target). "
                        "Ease off to stay on target."
                    ),
                )
            )

    fired: list[MidMonthAlert] = []
    for alert in candidates:
        already_sent = (
            await session.execute(
                select(AuditAlertSent).where(
                    AuditAlertSent.user_id == user_id,
                    AuditAlertSent.month == month_start,
                    AuditAlertSent.alert_type == alert.alert_type,
                )
            )
        ).scalar_one_or_none()
        if already_sent is not None:
            continue
        session.add(AuditAlertSent(user_id=user_id, month=month_start, alert_type=alert.alert_type))
        await session.commit()
        fired.append(alert)

    return fired
