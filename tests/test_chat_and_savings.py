"""Phase 3: the chat agent, Yes/No approvals (PendingAction), the savings
flow (own accounts, savings transfers/withdrawals, the month plan) and the
money alerts. The model is scripted (see tests/test_agent_core.py)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.telegram.handlers.commands as commands_module
import app.telegram.handlers.messages as messages_module
from app.agent_core.actions import resolve_pending_action
from app.agent_core.chat import answer_chat
from app.audit import build_snapshot
from app.db import Base
from app.ingestion import ingest_sms_transaction
from app.models import Budget, ConversationMessage, Expense, Income, OwnAccount, PendingAction, User
from app.month_plan import MonthPlan, check_money_alerts, get_month_plan, money_alerts_for
from app.parser import looks_like_bank_sms
from app.savings import account_direction, income_kind
from app.telegram.handlers import handle_ask_command, handle_message
from tests.test_agent_core import ScriptedChatProvider, answer, calls


class _NoLLM:
    async def parse_transaction(self, raw_text: str):
        raise AssertionError("the regex parser should handle these SMS on its own")


async def _user(session, chat_id: int = 1) -> User:
    user = User(telegram_chat_id=chat_id)
    session.add(user)
    await session.commit()
    return user


# --- chat agent -------------------------------------------------------------


async def test_chat_question_is_answered_from_a_tool_and_remembered(db_session):
    user = await _user(db_session)
    db_session.add(Expense(user_id=user.id, amount=Decimal("250"), category="Food", txn_date=date(2026, 11, 3)))
    await db_session.commit()
    provider = ScriptedChatProvider(
        [
            calls(
                (
                    "get_spending",
                    {
                        "date_range": "custom",
                        "custom_start": "2026-11-01",
                        "custom_end": "2026-11-30",
                        "category": "food",
                    },
                )
            ),
            answer("You spent Rs.250.00 on Food in November."),
        ]
    )

    reply = await answer_chat(db_session, provider, user.id, "how much on food this month?")

    assert reply.text == "You spent Rs.250.00 on Food in November."
    tool_result = provider.seen_messages[1][-1].result
    assert tool_result["total_spent"] == "250.00"
    saved = (await db_session.execute(select(ConversationMessage))).scalars().all()
    assert [m.role for m in saved] == ["user", "assistant"]


async def test_follow_up_question_sees_the_earlier_conversation(db_session):
    user = await _user(db_session)
    await answer_chat(db_session, ScriptedChatProvider([answer("Rs.100.")]), user.id, "food this week?")
    provider = ScriptedChatProvider([answer("Rs.80.")])

    await answer_chat(db_session, provider, user.id, "and last week?")

    goal = provider.seen_messages[0][0].text
    assert "food this week?" in goal
    assert "and last week?" in goal


async def test_chat_change_waits_for_yes_and_then_runs_in_plain_code(db_session):
    user = await _user(db_session)
    provider = ScriptedChatProvider(
        [calls(("set_budget", {"category": "shopping", "monthly_limit": "3000"})), answer("Please confirm below.")]
    )

    reply = await answer_chat(db_session, provider, user.id, "set my shopping budget to 3000")

    [action] = reply.confirmations
    assert action.question == "Set your Shopping budget to Rs.3,000.00 a month?"
    assert (await db_session.execute(select(func.count()).select_from(Budget))).scalar_one() == 0  # not yet

    _, message = await resolve_pending_action(db_session, action.id, user_id=user.id, approved=True)
    assert "Shopping budget set" in message
    budget = (await db_session.execute(select(Budget))).scalar_one()
    assert (budget.category, budget.monthly_limit) == ("Shopping", Decimal("3000"))

    again, message = await resolve_pending_action(db_session, action.id, user_id=user.id, approved=True)
    assert again is None and "already answered" in message


async def test_saying_no_changes_nothing(db_session):
    user = await _user(db_session)
    provider = ScriptedChatProvider([calls(("log_cash_expense", {"category": "Food", "amount": "90"})), answer("ok")])
    reply = await answer_chat(db_session, provider, user.id, "I spent 90 cash on chai")

    _, message = await resolve_pending_action(db_session, reply.confirmations[0].id, user_id=user.id, approved=False)

    assert "didn't change" in message
    assert (await db_session.execute(select(func.count()).select_from(Expense))).scalar_one() == 0


async def test_someone_else_cannot_approve_my_change(db_session):
    me = await _user(db_session, chat_id=1)
    stranger = await _user(db_session, chat_id=2)
    provider = ScriptedChatProvider([calls(("set_budget", {"category": "Food", "monthly_limit": "10"})), answer("ok")])
    reply = await answer_chat(db_session, provider, me.id, "food budget 10")

    action, _ = await resolve_pending_action(db_session, reply.confirmations[0].id, user_id=stranger.id, approved=True)

    assert action is None
    assert (await db_session.execute(select(func.count()).select_from(Budget))).scalar_one() == 0


async def test_invalid_chat_changes_are_denied_before_asking_the_user(db_session):
    user = await _user(db_session)
    provider = ScriptedChatProvider(
        [
            calls(
                ("set_budget", {"category": "Groceries", "monthly_limit": "100"}),
                ("set_budget", {"category": "Food", "monthly_limit": "0"}),
                ("recategorize_expense", {"expense_id": "00000000-0000-0000-0000-000000000000", "category": "Food"}),
                ("remember_savings_account", {"last4": "12a4"}),
            ),
            answer("Sorry."),
        ]
    )

    reply = await answer_chat(db_session, provider, user.id, "do things")

    assert reply.confirmations == []
    errors = [m.result["error"] for m in provider.seen_messages[1] if m.role == "tool"]
    assert len(errors) == 4
    assert (await db_session.execute(select(func.count()).select_from(PendingAction))).scalar_one() == 0


# --- routing ----------------------------------------------------------------


def test_bank_sms_and_chat_messages_are_told_apart_without_an_llm():
    assert looks_like_bank_sms("Rs.450.00 debited from A/c XX1234 on 10-11-26 to BLINKIT")
    assert looks_like_bank_sms("Dear UPI user A/C X1234 debited by 20.0 on date 22Nov26 trf to SWIGGY")
    assert not looks_like_bank_sms("Did my salary of Rs 50000 get credited?")
    assert not looks_like_bank_sms("why was Rs.1200 debited at Swiggy on 05-11-26?")
    assert not looks_like_bank_sms("I paid Rs 200 cash for lunch, please log it")
    assert not looks_like_bank_sms("how much did I spend on food this week?")
    assert not looks_like_bank_sms("set my shopping budget to 3000")


async def _patched_sessions(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(messages_module, "SessionLocal", factory)
    monkeypatch.setattr(commands_module, "SessionLocal", factory)
    return factory, engine


def _update(text: str, provider, args: list[str] | None = None):
    replies: list[str] = []
    sent: list[str] = []

    async def reply_text(message: str) -> None:
        replies.append(message)

    async def send_message(chat_id: int, text: str, reply_markup=None) -> SimpleNamespace:
        sent.append(text)
        return SimpleNamespace(message_id=1)

    update = SimpleNamespace(
        message=SimpleNamespace(text=text, reply_text=reply_text), effective_chat=SimpleNamespace(id=5)
    )
    context = SimpleNamespace(
        args=args, bot_data={"llm_provider": provider}, bot=SimpleNamespace(send_message=send_message)
    )
    return update, context, replies, sent


async def test_a_typed_question_goes_to_the_chat_agent(tmp_path, monkeypatch):
    _, engine = await _patched_sessions(tmp_path, monkeypatch)
    provider = ScriptedChatProvider([answer("You have Rs.0.00 left.")])
    update, context, replies, _ = _update("how much is left?", provider)

    await handle_message(update, context)

    assert replies == ["You have Rs.0.00 left."]
    await engine.dispose()


async def test_ask_command_uses_the_chat_agent(tmp_path, monkeypatch):
    _, engine = await _patched_sessions(tmp_path, monkeypatch)
    provider = ScriptedChatProvider([answer("Rs.10.00.")])
    update, context, replies, _ = _update("/ask x", provider, args=["food", "today?"])

    await handle_ask_command(update, context)

    assert replies == ["Rs.10.00."]
    await engine.dispose()


async def test_a_forwarded_sms_is_processed_right_away(tmp_path, monkeypatch):
    _, engine = await _patched_sessions(tmp_path, monkeypatch)
    provider = ScriptedChatProvider(
        [calls(("log_as_new_expense", {"category": "Food", "confidence": 0.95, "reason": "x"})), answer("ok")]
    )
    update, context, replies, _ = _update("Rs.380.00 debited from A/c XX1234 on 11-11-26 to SWIGGY", provider)

    await handle_message(update, context)

    assert "1 auto-logged" in replies[0]
    await engine.dispose()


# --- savings detection --------------------------------------------------------


def test_account_direction_reads_to_and_from():
    assert account_direction("debited for Rs.1500 and credited to a/c no. XXXXXXXX4321", "4321") == "to"
    assert account_direction("Sent Rs.40000 From HDFC Bank A/C *1234 To A/C *4321", "4321") == "to"
    assert account_direction("Rs.3000 credited to A/c XX1234 from A/c XX4321", "4321") == "from"
    assert account_direction("Rs.500 debited from A/c XX4321 to SWIGGY", "4321") == "from"
    assert account_direction("Rs.500 debited from A/c XX1234 to SWIGGY", "4321") is None


def test_income_kind_labels():
    assert income_kind("salary credited", Decimal("50000"), [], None, 0.05) == "salary"
    assert income_kind("Refund of Rs.299 credited", Decimal("299"), [], None, 0.05) == "refund"
    assert income_kind("Rs.3000 credited from A/c XX4321", Decimal("3000"), ["4321"], None, 0.05) == SW
    profile = SimpleNamespace(expected_salary=Decimal("50000"))
    assert income_kind("NEFT from EMPLOYER", Decimal("50100"), [], profile, 0.05) == "salary"
    assert income_kind("NEFT from FRIEND", Decimal("700"), [], profile, 0.05) == "other"


SW = "savings_withdrawal"


async def test_the_varying_savings_month_from_the_design_doc(db_session):
    """docs/AGENT_DESIGN.md §8: salary 50,000 → save 35,000, then 5,000 more
    → spend → take 3,000 back. No fixed percentage anywhere."""
    user = await _user(db_session)
    db_session.add(OwnAccount(user_id=user.id, last4="4321", kind="savings"))
    await db_session.commit()

    async def forward(sms: str):
        return await ingest_sms_transaction(db_session, user, sms, _NoLLM())

    salary = await forward("INR 50,000.00 credited to your A/c XX1234 on 01-11-26 by NEFT SALARY from TARKA LABS")
    assert (salary.outcome, salary.income_kind) == ("income_recorded", "salary")

    first = await forward("Rs.35,000.00 debited from A/c XX1234 on 01-11-26 and credited to A/c XX4321")
    second = await forward("Rs.5,000.00 debited from A/c XX1234 on 10-11-26 and credited to A/c XX4321")
    assert first.outcome == second.outcome == "savings_transfer"

    db_session.add(Expense(user_id=user.id, amount=Decimal("13000"), category="Food", txn_date=date(2026, 11, 15)))
    await db_session.commit()

    back = await forward("Rs.3,000.00 credited to A/c XX1234 on 20-11-26 from A/c XX4321")
    assert back.outcome == "savings_withdrawal"

    plan = await get_month_plan(db_session, user.id, today=date(2026, 11, 20))
    assert plan.salary == Decimal("50000")
    assert plan.moved_to_savings == Decimal("40000")
    assert plan.taken_back_from_savings == Decimal("3000")
    assert plan.saved == Decimal("37000")
    assert plan.saved_pct == Decimal("74.0")
    assert plan.spending_money == Decimal("13000")  # 50,000 − 40,000 + 3,000
    assert plan.money_left == Decimal("0")

    # The savings moves are not spending and the withdrawal is not income.
    savings_rows = (await db_session.execute(select(Expense).where(Expense.category == "Savings"))).scalars().all()
    assert len(savings_rows) == 2
    incomes = (await db_session.execute(select(Income))).scalars().all()
    assert sorted(i.kind for i in incomes) == ["salary", SW]


async def test_spending_before_saving_uses_the_whole_salary(db_session):
    user = await _user(db_session)
    db_session.add(
        Income(user_id=user.id, amount=Decimal("50000"), txn_date=date(2026, 11, 1), raw_text="s", kind="salary")
    )
    await db_session.commit()
    plan = await get_month_plan(db_session, user.id, today=date(2026, 11, 3))
    assert plan.spending_money == Decimal("50000")
    assert "Nothing moved to savings yet" in plan.as_message()


def _plan(spent: str, spending_money: str = "10000", day: int = 10) -> MonthPlan:
    return MonthPlan(
        month_start=date(2026, 11, 1),
        month_end=date(2026, 11, 30),
        today=date(2026, 11, day),
        salary=Decimal(spending_money),
        other_income=Decimal("0"),
        moved_to_savings=Decimal("0"),
        taken_back_from_savings=Decimal("0"),
        spent=Decimal(spent),
    )


def test_money_alert_thresholds():
    assert [a.alert_type for a in money_alerts_for(_plan("4000", day=25))] == []
    assert [a.alert_type for a in money_alerts_for(_plan("5000", day=25))] == ["half_used"]
    assert [a.alert_type for a in money_alerts_for(_plan("8500", day=28))] == ["running_low"]
    assert [a.alert_type for a in money_alerts_for(_plan("10500", day=28))] == ["ran_out"]
    # 6,000 spent by day 10 → 600/day → the remaining 4,000 lasts ~6 days → before month end
    assert "fast_pace" in [a.alert_type for a in money_alerts_for(_plan("6000", day=10))]
    assert money_alerts_for(_plan("6000", spending_money="0")) == []  # no salary yet


async def test_each_money_alert_is_sent_once_a_month(db_session):
    user = await _user(db_session)
    db_session.add_all(
        [
            Income(user_id=user.id, amount=Decimal("10000"), txn_date=date(2026, 11, 1), raw_text="s", kind="salary"),
            Expense(user_id=user.id, amount=Decimal("5000"), category="Food", txn_date=date(2026, 11, 20)),
        ]
    )
    await db_session.commit()

    first = await check_money_alerts(db_session, user.id, today=date(2026, 11, 25))
    second = await check_money_alerts(db_session, user.id, today=date(2026, 11, 26))

    assert [a.alert_type for a in first] == ["half_used"]
    assert second == []


def test_audit_does_not_count_a_savings_withdrawal_as_income():
    incomes = [
        SimpleNamespace(amount=Decimal("50000"), kind="salary"),
        SimpleNamespace(amount=Decimal("3000"), kind=SW),
    ]
    snapshot = build_snapshot(
        date(2026, 10, 1), date(2026, 10, 31), incomes, [], [], [], None, salary_tolerance_pct=0.05
    )
    assert snapshot.total_income == Decimal("50000")
    assert snapshot.taken_back_from_savings == Decimal("3000")


# --- fixes from the Phase 3 review --------------------------------------------


def test_budget_month_starts_on_payday():
    from app.month_plan import plan_period

    assert plan_period(date(2026, 11, 15), None) == (date(2026, 11, 1), date(2026, 11, 30))
    assert plan_period(date(2026, 11, 15), 1) == (date(2026, 11, 1), date(2026, 11, 30))
    assert plan_period(date(2026, 11, 29), 28) == (date(2026, 11, 28), date(2026, 12, 27))
    assert plan_period(date(2026, 12, 5), 28) == (date(2026, 11, 28), date(2026, 12, 27))
    assert plan_period(date(2026, 3, 10), 28) == (date(2026, 2, 28), date(2026, 3, 27))


async def test_salary_paid_on_the_28th_counts_for_the_month_it_is_spent_in(db_session):
    from app.models import SalaryProfile

    user = await _user(db_session)
    db_session.add(SalaryProfile(user_id=user.id, expected_salary=Decimal("50000"), payday_day=28))
    db_session.add(
        Income(user_id=user.id, amount=Decimal("50000"), txn_date=date(2026, 11, 28), raw_text="s", kind="salary")
    )
    await db_session.commit()

    plan = await get_month_plan(db_session, user.id, today=date(2026, 12, 3))

    assert plan.salary == Decimal("50000")
    assert plan.month_start == date(2026, 11, 28)


@pytest.mark.parametrize(
    "sms",
    [
        "Rs 35,000.00 credited to A/c XX4321 on 01-11-26 from A/c XX1234 via IMPS",
        "Rs.3,000.00 transferred from A/c XX4321 on 20-11-26 to A/c XX1234",
    ],
)
async def test_the_savings_banks_own_side_of_a_transfer_is_skipped(db_session, sms):
    user = await _user(db_session)
    db_session.add(OwnAccount(user_id=user.id, last4="4321", kind="savings"))
    await db_session.commit()

    result = await ingest_sms_transaction(db_session, user, sms, _NoLLM())

    assert result.outcome == "savings_side_skipped"
    assert (await db_session.execute(select(func.count()).select_from(Income))).scalar_one() == 0
    assert (await db_session.execute(select(func.count()).select_from(Expense))).scalar_one() == 0


async def test_spend_from_the_savings_account_to_a_shop_is_still_spending(db_session):
    user = await _user(db_session)
    db_session.add(OwnAccount(user_id=user.id, last4="4321", kind="savings"))
    await db_session.commit()

    result = await ingest_sms_transaction(
        db_session, user, "Rs.500 sent to Ramesh via UPI on 12-11-26. A/c XX4321 debited.", _NoLLM()
    )

    assert result.outcome == "transaction_created"


async def test_double_tap_on_yes_applies_the_change_once(db_session):
    user = await _user(db_session)
    provider = ScriptedChatProvider([calls(("log_cash_expense", {"category": "Food", "amount": "90"})), answer("ok")])
    reply = await answer_chat(db_session, provider, user.id, "log 90 food")
    action_id = reply.confirmations[0].id

    first, _ = await resolve_pending_action(db_session, action_id, user_id=user.id, approved=True)
    second, _ = await resolve_pending_action(db_session, action_id, user_id=user.id, approved=True)

    assert first is not None and second is None
    assert (await db_session.execute(select(func.count()).select_from(Expense))).scalar_one() == 1
