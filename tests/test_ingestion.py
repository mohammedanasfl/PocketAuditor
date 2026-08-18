"""app.ingestion.ingest_sms_transaction — extracted from the Telegram text
handler (app/telegram/handlers/messages.py:handle_message), which previously
had no dedicated test coverage of its own. Parsing itself (regex vs LLM
fallback) is already exhaustively covered by tests/test_parser.py; these
tests only check what happens to the parsed result once it reaches this
service function: a debit becomes a pending Transaction, a credit becomes an
Income row (Phase 4 — it used to be discarded), and a ParseError is surfaced
as an outcome rather than raised.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.ingestion import ingest_sms_transaction
from app.llm.base import LLMDecisionError
from app.models import Income, Transaction, User


class _NeverCalledProvider:
    """Fails loudly if the regex pass wasn't confident enough to skip the
    LLM — same guard tests/test_parser.py uses for the same reason."""

    async def parse_transaction(self, raw_text: str):  # pragma: no cover
        raise AssertionError(f"LLM fallback should not have been invoked for: {raw_text!r}")


class _AlwaysFailsProvider:
    async def parse_transaction(self, raw_text: str):
        raise LLMDecisionError("model could not find a transaction")


async def _make_user(session) -> User:
    user = User(telegram_chat_id=24680)
    session.add(user)
    await session.commit()
    return user


async def test_debit_sms_creates_pending_transaction(db_session):
    user = await _make_user(db_session)
    text = "Rs.450 debited from A/c XX1234 towards BLINKIT on 10-08-26. Avl Bal Rs.900.00 -HDFC Bank"

    result = await ingest_sms_transaction(db_session, user, text, _NeverCalledProvider())

    assert result.outcome == "transaction_created"
    assert result.amount == Decimal("450.00")
    assert result.merchant == "BLINKIT"
    assert result.txn_date == date(2026, 8, 10)

    stored = (await db_session.execute(select(Transaction).where(Transaction.user_id == user.id))).scalar_one()
    assert stored.status == "pending"
    assert stored.source == "sms"
    assert stored.amount == Decimal("450.00")


async def test_credit_sms_records_income_and_creates_no_transaction(db_session):
    user = await _make_user(db_session)
    text = "INR 25,000.00 credited to your A/c XX1234 on 01-08-26 by NEFT from EMPLOYER PVT LTD -HDFC Bank"

    result = await ingest_sms_transaction(db_session, user, text, _NeverCalledProvider())

    assert result.outcome == "income_recorded"
    assert result.amount == Decimal("25000.00")
    assert result.merchant == "EMPLOYER PVT LTD"
    assert result.txn_date == date(2026, 8, 1)

    # A credit becomes an Income row, never a Transaction — it must stay out of
    # the reconcile loop and out of /spend.
    incomes = (await db_session.execute(select(Income).where(Income.user_id == user.id))).scalars().all()
    assert len(incomes) == 1
    assert incomes[0].amount == Decimal("25000.00")
    assert incomes[0].source == "EMPLOYER PVT LTD"
    assert incomes[0].created_via == "auto"

    transactions = (await db_session.execute(select(Transaction).where(Transaction.user_id == user.id))).scalars().all()
    assert transactions == []


async def test_unparseable_message_returns_parse_error_outcome(db_session):
    user = await _make_user(db_session)
    text = "Reminder: Please complete your KYC update by visiting the nearest branch. -Bank"

    result = await ingest_sms_transaction(db_session, user, text, _AlwaysFailsProvider())

    assert result.outcome == "parse_error"

    stored = (await db_session.execute(select(Transaction).where(Transaction.user_id == user.id))).scalars().all()
    assert stored == []
