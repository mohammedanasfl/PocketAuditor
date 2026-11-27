"""Transaction-ingestion service functions: turn an incoming SMS or receipt
photo into a `transactions` row. Each returns an outcome dataclass rather
than raising or replying directly, so the (Telegram-specific) caller just
branches on `.outcome` to pick reply text — no Telegram types appear here.

Kept separate from app.expenses, which only ever creates `expenses` — these
never write directly to the ledger, only to the pending-transaction queue
app.agent.reconcile_user later drains.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.categories import normalize_category
from app.income import record_income
from app.llm.base import LLMDecisionError, LLMProvider
from app.models import Income, Transaction, User
from app.parser import ParseError, parse_sms
from app.timeutil import local_today

logger = logging.getLogger(__name__)


async def _sms_already_saved(session: AsyncSession, user_id: UUID, raw_text: str, txn_date: date) -> bool:
    """True if this exact SMS, for the same date, is already saved as a
    transaction or an income. The date matters: a short app notification
    like "Paid Rs.20 to Chai Point" can honestly repeat on another day."""
    txn_match = await session.execute(
        select(Transaction.id)
        .where(Transaction.user_id == user_id, Transaction.txn_date == txn_date, Transaction.raw_text == raw_text)
        .limit(1)
    )
    if txn_match.first() is not None:
        return True
    income_match = await session.execute(
        select(Income.id)
        .where(Income.user_id == user_id, Income.txn_date == txn_date, Income.raw_text == raw_text)
        .limit(1)
    )
    return income_match.first() is not None


@dataclass
class SmsIngestResult:
    outcome: Literal["transaction_created", "income_recorded", "parse_error", "already_seen"]
    amount: Decimal | None = None
    merchant: str | None = None
    txn_date: date | None = None


async def ingest_sms_transaction(
    session: AsyncSession, user: User, raw_text: str, provider: LLMProvider
) -> SmsIngestResult:
    """Plain-text message handling: treat as forwarded SMS/UPI text."""
    try:
        parsed = await parse_sms(raw_text, provider)
    except ParseError:
        return SmsIngestResult(outcome="parse_error")

    if await _sms_already_saved(session, user.id, raw_text, parsed.txn_date):
        # The same SMS forwarded twice would otherwise count the money twice
        # (a salary forwarded twice doubles your income).
        logger.info("user=%s: this exact SMS was already saved, skipping", user.id)
        return SmsIngestResult(outcome="already_seen")

    if not parsed.is_debit:
        # Money-in: record it in the incomes ledger for the monthly salary
        # audit rather than dropping it. Credits never enter the transactions/
        # reconcile loop — see app/income.py.
        await record_income(
            session,
            user.id,
            amount=parsed.amount,
            source=parsed.merchant,
            txn_date=parsed.txn_date,
            raw_text=raw_text,
            created_via="auto",
        )
        logger.info("user=%s: recorded income of Rs.%s from %r", user.id, parsed.amount, parsed.merchant)
        return SmsIngestResult(
            outcome="income_recorded", amount=parsed.amount, merchant=parsed.merchant, txn_date=parsed.txn_date
        )

    txn = Transaction(
        user_id=user.id,
        raw_text=raw_text,
        amount=parsed.amount,
        merchant=parsed.merchant,
        txn_date=parsed.txn_date,
        source="sms",
        status="pending",
    )
    session.add(txn)
    await session.commit()
    logger.info(
        "user=%s: created transaction=%s amount=%s merchant=%r txn_date=%s (parsed via %s, confidence=%.2f)",
        user.id,
        txn.id,
        parsed.amount,
        parsed.merchant,
        parsed.txn_date,
        parsed.method,
        parsed.confidence,
    )
    return SmsIngestResult(
        outcome="transaction_created", amount=parsed.amount, merchant=parsed.merchant, txn_date=parsed.txn_date
    )


@dataclass
class PhotoIngestResult:
    outcome: Literal["transaction_created", "not_implemented", "llm_error", "low_confidence"]
    amount: float | None = None
    merchant: str | None = None
    txn_date: date | None = None
    category_hint: str | None = None


async def ingest_photo_transaction(
    session: AsyncSession,
    user: User,
    image_bytes: bytes,
    caption: str | None,
    provider: LLMProvider,
    confidence_threshold: float,
) -> PhotoIngestResult:
    """Photo message handling: treat as a bill/receipt/UPI screenshot. Feeds
    the same `transactions` table and `reconcile_user` loop as SMS text —
    the only difference is `source='photo'` and how the row gets populated."""
    try:
        receipt = await provider.extract_receipt(image_bytes, "image/jpeg")
    except NotImplementedError:
        return PhotoIngestResult(outcome="not_implemented")
    except LLMDecisionError as exc:
        logger.warning("user=%s: extract_receipt failed: %s", user.id, exc)
        return PhotoIngestResult(outcome="llm_error")

    if not receipt.readable or receipt.confidence < confidence_threshold or receipt.total_amount is None:
        logger.info(
            "user=%s: photo receipt not trustworthy (readable=%s confidence=%.2f total_amount=%s)",
            user.id,
            receipt.readable,
            receipt.confidence,
            receipt.total_amount,
        )
        return PhotoIngestResult(outcome="low_confidence")

    txn_date = receipt.txn_date or local_today()
    # A caption is the user explicitly telling us the category up front (e.g.
    # sending a receipt photo with "Food" as the caption) — trust it over
    # letting the agent loop guess one later. normalize_category rejects
    # anything that isn't one of the known categories rather than silently
    # accepting a typo as a brand-new one.
    category_hint = normalize_category(caption) if caption else None
    raw_text = f"[Photo receipt] merchant={receipt.merchant!r} line_items={receipt.line_items or []}"

    txn = Transaction(
        user_id=user.id,
        raw_text=raw_text,
        amount=Decimal(str(receipt.total_amount)),
        merchant=receipt.merchant,
        txn_date=txn_date,
        source="photo",
        category_hint=category_hint,
        status="pending",
    )
    session.add(txn)
    await session.commit()
    logger.info(
        "user=%s: created transaction=%s amount=%s merchant=%r txn_date=%s category_hint=%r "
        "(source=photo, confidence=%.2f)",
        user.id,
        txn.id,
        receipt.total_amount,
        receipt.merchant,
        txn_date,
        category_hint,
        receipt.confidence,
    )
    return PhotoIngestResult(
        outcome="transaction_created",
        amount=receipt.total_amount,
        merchant=receipt.merchant,
        txn_date=txn_date,
        category_hint=category_hint,
    )
