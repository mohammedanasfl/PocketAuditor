"""Regression tests for the Phase 0 bug fixes (see docs/AGENT_DESIGN.md §10).

Each test is named after the bug it protects against, so a failure here
says exactly which old bug came back.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import anthropic
import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.cron as cron_module
from app.agent import reconcile_user
from app.audit import build_snapshot
from app.config import settings
from app.db import Base, insert_once
from app.expenses import recategorize_expense, resolve_ask_user_answer
from app.ingestion import ingest_sms_transaction
from app.llm.base import LLMDecisionError
from app.llm.claude import ClaudeProvider
from app.llm.gemini import GeminiProvider
from app.models import BudgetAlertSent, Expense, Income, ReconciliationRun, Transaction, User
from app.parser import ParseError, parse_sms
from app.privacy import redact
from app.schemas import LLMExtraction, MatchDecision
from app.telegram.access import is_chat_allowed
from app.timeutil import local_today


class _NoLLM:
    """Fails the test if the parser calls the LLM."""

    async def parse_transaction(self, raw_text: str) -> LLMExtraction:
        raise AssertionError("the LLM should not have been called")


class _NotATransactionLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.texts: list[str] = []

    async def parse_transaction(self, raw_text: str) -> LLMExtraction:
        self.calls += 1
        self.texts.append(raw_text)
        return LLMExtraction(is_transaction=False, amount=None, merchant=None, txn_date=None, is_debit=None)


# --- parser ---------------------------------------------------------------


async def test_credit_sms_that_also_says_transferred_is_income_not_spend():
    text = "Your A/c XX1234 is credited with Rs.5000.00 on 12-11-26, transferred from RAHUL KUMAR. Avl Bal Rs.20000"
    result = await parse_sms(text, _NoLLM())
    assert result.is_debit is False
    assert result.amount == Decimal("5000.00")


async def test_balance_written_before_the_amount_is_not_used_as_the_amount():
    text = "Avl Bal Rs.12,000.00. Rs.500 debited from A/c XX12 on 12-11-26 to ZOMATO"
    result = await parse_sms(text, _NoLLM())
    assert result.amount == Decimal("500.00")
    assert result.merchant == "ZOMATO"


async def test_otp_message_is_rejected_without_calling_the_llm():
    text = "Your OTP for payment of Rs.2000 at FLIPKART is 123456. Do not share."
    with pytest.raises(ParseError):
        await parse_sms(text, _NoLLM())


async def test_message_with_no_debit_or_credit_word_goes_to_the_llm_instead_of_guessing():
    # Amount + merchant = 0.75 confidence, but no direction word: the parser
    # used to assume "debit" here. Now it asks the LLM.
    provider = _NotATransactionLLM()
    with pytest.raises(ParseError):
        await parse_sms("Get Rs.100 cashback at MYNTRA this weekend", provider)
    assert provider.calls == 1


async def test_sms_sent_to_the_llm_has_long_numbers_masked():
    provider = _NotATransactionLLM()
    with pytest.raises(ParseError):
        await parse_sms("Ref 117001580517 call 9876543210 about Rs.50", provider)
    assert "117001580517" not in provider.texts[0]
    assert "9876543210" not in provider.texts[0]


def test_redact_masks_long_numbers_but_keeps_amounts():
    text = "Rs.1,250.00 paid. UPI Ref 117001580517. Call 18002586161. Amount 50000"
    redacted = redact(text)
    assert "117001580517" not in redacted
    assert "18002586161" not in redacted
    assert "Rs.1,250.00" in redacted
    assert "50000" in redacted


# --- timezone -------------------------------------------------------------


def test_local_today_uses_the_configured_timezone(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "Asia/Kolkata")
    assert local_today() == datetime.now(ZoneInfo("Asia/Kolkata")).date()


# --- access ---------------------------------------------------------------


def test_allowlist_is_closed_by_default_and_opens_only_for_listed_chats():
    assert is_chat_allowed(42, "") is False
    assert is_chat_allowed(42, "42") is True
    assert is_chat_allowed(42, " 7, 42 ") is True
    assert is_chat_allowed(43, "7,42") is False
    assert is_chat_allowed(43, "*") is True


# --- audit ----------------------------------------------------------------


def test_savings_rate_is_capped_so_it_fits_the_database_column():
    incomes = [SimpleNamespace(amount=Decimal("1.00"), kind="other")]
    expenses = [
        SimpleNamespace(
            id=uuid4(), amount=Decimal("50000.00"), category="Food", merchant="X", txn_date=date(2026, 10, 3)
        )
    ]
    snapshot = build_snapshot(
        date(2026, 10, 1), date(2026, 10, 31), incomes, expenses, [], [], None, salary_tolerance_pct=0.05
    )
    assert snapshot.savings_rate == Decimal("-99999.99")


async def test_insert_once_skips_a_duplicate_marker_instead_of_crashing(db_session):
    user = User(telegram_chat_id=1)
    db_session.add(user)
    await db_session.commit()
    first = BudgetAlertSent(user_id=user.id, category="Food", month=date(2026, 11, 1))
    second = BudgetAlertSent(user_id=user.id, category="Food", month=date(2026, 11, 1))
    assert await insert_once(db_session, first) is True
    assert await insert_once(db_session, second) is False


# --- ownership ------------------------------------------------------------


async def _user_with_open_question(session, chat_id: int) -> tuple[User, ReconciliationRun]:
    user = User(telegram_chat_id=chat_id)
    session.add(user)
    await session.commit()
    txn = Transaction(user_id=user.id, raw_text="x", amount=Decimal("10"), merchant="SHOP", txn_date=date(2026, 11, 1))
    session.add(txn)
    await session.commit()
    run = ReconciliationRun(user_id=user.id, transaction_id=txn.id, decision="ask_user", reasoning="?", status="open")
    session.add(run)
    await session.commit()
    return user, run


async def test_a_user_cannot_answer_someone_elses_question(db_session):
    _owner, run = await _user_with_open_question(db_session, chat_id=1)
    stranger = User(telegram_chat_id=2)
    db_session.add(stranger)
    await db_session.commit()

    assert await resolve_ask_user_answer(db_session, run.id, "Food", user_id=stranger.id) is None
    await db_session.refresh(run)
    assert run.status == "open"


async def test_a_user_cannot_recategorize_someone_elses_expense(db_session):
    owner = User(telegram_chat_id=1)
    stranger = User(telegram_chat_id=2)
    db_session.add_all([owner, stranger])
    await db_session.commit()
    expense = Expense(user_id=owner.id, amount=Decimal("10"), category="Food", txn_date=date(2026, 11, 1))
    db_session.add(expense)
    await db_session.commit()

    assert await recategorize_expense(db_session, expense.id, "Bills", user_id=stranger.id) is None
    await db_session.refresh(expense)
    assert expense.category == "Food"


async def test_malformed_button_data_does_not_crash(tmp_path, monkeypatch):
    import app.telegram.handlers.callbacks as callbacks_module

    edits: list[str] = []

    async def answer() -> None:
        return None

    async def edit_message_text(text: str) -> None:
        edits.append(text)

    query = SimpleNamespace(data="cat:not-a-uuid:food", answer=answer, edit_message_text=edit_message_text)
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=1))
    await callbacks_module.handle_category_callback(update, SimpleNamespace())
    assert edits == []


# --- duplicate SMS --------------------------------------------------------


async def test_forwarding_the_same_salary_sms_twice_counts_it_once(db_session):
    user = User(telegram_chat_id=1)
    db_session.add(user)
    await db_session.commit()
    sms = "INR 50,000.00 credited to your A/c XX1234 on 01-11-26 by NEFT from EMPLOYER PVT LTD"

    first = await ingest_sms_transaction(db_session, user, sms, _NoLLM())
    second = await ingest_sms_transaction(db_session, user, sms, _NoLLM())

    assert first.outcome == "income_recorded"
    assert second.outcome == "already_seen"
    count = (await db_session.execute(select(func.count()).select_from(Income))).scalar_one()
    assert count == 1


# --- provider errors ------------------------------------------------------


async def test_claude_network_error_becomes_llm_decision_error(monkeypatch):
    provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")

    async def broken_create(**kwargs):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))

    monkeypatch.setattr(provider._client.messages, "create", broken_create)
    with pytest.raises(LLMDecisionError):
        await provider.parse_transaction("Rs.10 debited")


async def test_gemini_network_error_becomes_llm_decision_error(monkeypatch):
    provider = GeminiProvider(api_key="test-key", model="gemini-3.5-flash-lite")

    async def broken_generate(**kwargs):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(provider._client.aio.models, "generate_content", broken_generate)
    with pytest.raises(LLMDecisionError):
        await provider.parse_transaction("Rs.10 debited")


# --- cron -----------------------------------------------------------------


async def test_one_failing_user_does_not_stop_the_cron_for_everyone_else(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_chat_ids", "*")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cron.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(cron_module, "SessionLocal", session_factory)

    async with session_factory() as session:
        session.add_all([User(telegram_chat_id=1), User(telegram_chat_id=2)])
        await session.commit()

    sent_to: list[int] = []

    async def send_message(chat_id: int, text: str) -> None:
        if chat_id == 1:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        sent_to.append(chat_id)

    application = SimpleNamespace(bot=SimpleNamespace(send_message=send_message), bot_data={"llm_provider": None})
    await cron_module.reconcile_all_users(application)

    assert sent_to == [2]
    await engine.dispose()


# --- reconcile concurrency ------------------------------------------------


class _SlowAutoLogProvider:
    async def decide_match(self, transaction: dict, candidates: list[dict]) -> MatchDecision:
        await asyncio.sleep(0.05)  # give a parallel run the chance to interleave
        return MatchDecision(
            action="auto_log", matched_expense_id=None, suggested_category="Food", confidence=0.95, reasoning="ok"
        )


async def test_two_reconcile_runs_at_once_do_not_process_a_transaction_twice(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lock.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with session_factory() as session:
        user = User(telegram_chat_id=1)
        session.add(user)
        await session.commit()
        session.add(
            Transaction(
                user_id=user.id, raw_text="x", amount=Decimal("99"), merchant="CAFE", txn_date=date(2026, 11, 2)
            )
        )
        await session.commit()

    async def run_once() -> None:
        async with session_factory() as session:
            await reconcile_user(session, _SlowAutoLogProvider(), user.id)

    await asyncio.gather(run_once(), run_once())

    async with session_factory() as session:
        expenses = (await session.execute(select(func.count()).select_from(Expense))).scalar_one()
    assert expenses == 1
    await engine.dispose()


# --- fixes from the Phase 0 review ------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ICICI Bank Credit Card XX1234 debited for INR 500.00 on 12-11-26 at AMAZON",
        "Your SBI Credit Card ending 1234: Rs.500 spent at SWIGGY on 12-11-26",
    ],
)
async def test_credit_card_spend_is_a_debit_not_income(text):
    result = await parse_sms(text, _NoLLM())
    assert result.is_debit is True
    assert result.amount == Decimal("500.00")


async def test_credit_card_bill_reminder_is_not_income():
    provider = _NotATransactionLLM()
    with pytest.raises(ParseError):
        await parse_sms("Your credit card bill of Rs.12,345 is due on 25-11-26. Pay now.", provider)


async def test_real_alert_with_never_share_otp_footer_is_not_rejected():
    text = "Rs.500.00 debited from A/c XX1234 on 12-11-26 to SWIGGY. Never share your OTP/PIN with anyone."
    result = await parse_sms(text, _NoLLM())
    assert result.amount == Decimal("500.00")


async def test_comma_after_dear_customer_does_not_hide_the_amount():
    text = "Dear Customer, INR 50,000.00 credited to your A/c XX1234 on 01-11-26 by NEFT from EMPLOYER LTD"
    result = await parse_sms(text, _NoLLM())
    assert result.amount == Decimal("50000.00")
    assert result.is_debit is False


@pytest.mark.parametrize(
    "text",
    [
        "Avl Lmt Rs 50,000. Rs 999 spent at FLIPKART on 12-11-26",
        "Your A/c has a balance of Rs.5,000 after Rs.999 debited to FLIPKART on 12-11-26",
    ],
)
async def test_limit_and_balance_of_wording_is_skipped(text):
    result = await parse_sms(text, _NoLLM())
    assert result.amount == Decimal("999.00")


async def test_llm_that_cannot_tell_direction_is_not_guessed_as_debit():
    class _NoDirectionLLM:
        async def parse_transaction(self, raw_text: str) -> LLMExtraction:
            return LLMExtraction(is_transaction=True, amount=100.0, merchant="X", txn_date=None, is_debit=None)

    with pytest.raises(ParseError):
        await parse_sms("Rs.100 X", _NoDirectionLLM())


def test_redact_masks_phone_card_and_tollfree_numbers_with_separators():
    text = "Call 1800-258-6161 or +91 98765 43210. Card 4111 1111 1111 1111. Paid Rs.1,250.00"
    redacted = redact(text)
    assert "6161" not in redacted
    assert "43210" not in redacted
    assert "4111" not in redacted
    assert "Rs.1,250.00" in redacted


async def test_same_short_notification_on_a_different_day_is_not_a_duplicate(db_session):
    user = User(telegram_chat_id=1)
    db_session.add(user)
    await db_session.commit()
    db_session.add(
        Transaction(
            user_id=user.id, raw_text="Paid Rs.20 to Chai Point", amount=Decimal("20"), txn_date=date(2020, 1, 1)
        )
    )
    await db_session.commit()

    class _PaidLLM:
        async def parse_transaction(self, raw_text: str) -> LLMExtraction:
            return LLMExtraction(is_transaction=True, amount=20.0, merchant="Chai Point", txn_date=None, is_debit=True)

    result = await ingest_sms_transaction(db_session, user, "Paid Rs.20 to Chai Point", _PaidLLM())
    assert result.outcome == "transaction_created"


async def test_reconcile_does_not_ask_the_same_question_twice(db_session):
    class _AskProvider:
        calls = 0

        async def decide_match(self, transaction: dict, candidates: list[dict]) -> MatchDecision:
            _AskProvider.calls += 1
            return MatchDecision(
                action="ask_user", matched_expense_id=None, suggested_category=None, confidence=0.3, reasoning="?"
            )

    user = User(telegram_chat_id=1)
    db_session.add(user)
    await db_session.commit()
    db_session.add(Transaction(user_id=user.id, raw_text="x", amount=Decimal("5"), txn_date=date(2026, 11, 1)))
    await db_session.commit()

    first = await reconcile_user(db_session, _AskProvider(), user.id)
    second = await reconcile_user(db_session, _AskProvider(), user.id)
    assert first.asked_user == 1
    assert second.asked_user == 0
    assert _AskProvider.calls == 1


def test_unknown_timezone_fails_at_startup():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(database_url="x", telegram_bot_token="y", timezone="Mars/Olympus")
