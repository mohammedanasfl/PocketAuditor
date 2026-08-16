"""SQLAlchemy 2.0 declarative models.

Uses the generic Uuid / Numeric / DateTime types rather than postgresql.*
variants — identical DDL on Postgres (Neon), but lets the test suite run
against in-memory aiosqlite with no Docker dependency.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    """Bridges a Telegram chat to an internal UUID user id.

    Not in the original brief's schema — added so transactions/expenses can
    reference a stable UUID instead of coupling the ledger to Telegram's
    integer chat_id directly, and so per-user settings have somewhere to live
    later (timezone, default categories, opt-outs).
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Transaction(Base):
    """Raw parsed entries from forwarded SMS text."""

    __tablename__ = "transactions"
    __table_args__ = (Index("ix_transactions_user_status", "user_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    merchant: Mapped[str | None] = mapped_column(Text, nullable=True)
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="sms", server_default="sms")
    # The user's own explicit category choice (e.g. from a receipt photo's
    # caption) — not a guess. app.agent trusts this over the model's own
    # suggested_category when set; see app/agent.py:_apply_category_hint.
    category_hint: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 'pending' | 'processed' — not in the original brief; needed so the agent
    # loop and /reconcile can find unprocessed rows without a fragile join.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Expense(Base):
    """The actual ledger."""

    __tablename__ = "expenses"
    __table_args__ = (
        Index("ix_expenses_user_date_amount", "user_id", "txn_date", "amount"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    merchant: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    linked_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("transactions.id"), nullable=True, index=True
    )
    # 'manual' | 'auto_link' | 'auto_log'
    created_via: Mapped[str] = mapped_column(String(20), nullable=False, default="manual", server_default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReconciliationRun(Base):
    """The agent's decision trail — one row per transaction processed, for auditability."""

    __tablename__ = "reconciliation_runs"
    __table_args__ = (Index("ix_reconciliation_runs_user_status", "user_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("transactions.id"), nullable=False)
    # 'auto_link' | 'auto_log' | 'asked_user'
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    # 'open' | 'resolved' — an asked_user run starts 'open' and is flipped to
    # 'resolved' when the user answers the inline-keyboard question.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="resolved", server_default="resolved")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Telegram message id of the "which category?" prompt, so the callback
    # handler can edit it (strip the buttons) once answered.
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Budget(Base):
    """Per-category monthly spending limit, set via /setbudget. Phase 3a."""

    __tablename__ = "budgets"
    __table_args__ = (Index("ix_budgets_user_category", "user_id", "category", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    monthly_limit: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BudgetAlertSent(Base):
    """Tracks which (user, category, month) combinations already had an
    80%+ budget alert sent, so /check-budgets doesn't re-alert every run.
    `month` is always the first-of-month date, so the unique index below
    enforces "at most one alert per category per calendar month" directly."""

    __tablename__ = "budget_alerts_sent"
    __table_args__ = (
        Index("ix_budget_alerts_sent_user_category_month", "user_id", "category", "month", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    month: Mapped[date] = mapped_column(Date, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
