"""Stage 3 test (Phase 2 brief): a source='photo' transaction must flow
through reconcile_user identically to a source='sms' one, GIVEN the same
category_hint. agent.py's perceive/compare/decide/act loop doesn't otherwise
branch on Transaction.source — this test is the proof of that, exercised
through all three actions plus the confidence guard.

One deliberate exception exists: a photo with NO category_hint (no caption,
or an unrecognized one) gets its auto_log downgraded to ask_user rather than
letting the model guess a category for an unfamiliar merchant — that's not a
"source" special-case so much as photos being the only source that can carry
an explicit user-provided category at all. Covered separately in
tests/test_agent.py, not here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.agent import reconcile_user
from app.models import Expense, ReconciliationRun, Transaction, User
from app.schemas import MatchDecision


class _ScriptedProvider:
    def __init__(self, decisions: list[MatchDecision]):
        self._decisions = list(decisions)

    async def decide_match(self, transaction: dict, candidates: list[dict]) -> MatchDecision:
        return self._decisions.pop(0)

    async def parse_transaction(self, raw_text: str):  # pragma: no cover
        raise AssertionError("not used by the agent loop")


async def _make_user(session) -> User:
    user = User(telegram_chat_id=777)
    session.add(user)
    await session.commit()
    return user


async def _make_expense(session, user: User, *, amount: str, category: str, txn_date: date) -> Expense:
    expense = Expense(user_id=user.id, amount=Decimal(amount), category=category, txn_date=txn_date)
    session.add(expense)
    await session.commit()
    return expense


async def _make_transaction(
    session,
    user: User,
    *,
    amount: str,
    merchant: str,
    txn_date: date,
    source: str,
    category_hint: str | None = None,
) -> Transaction:
    txn = Transaction(
        user_id=user.id,
        raw_text=f"[{source}] Rs.{amount} at {merchant}",
        amount=Decimal(amount),
        merchant=merchant,
        txn_date=txn_date,
        source=source,
        category_hint=category_hint,
        status="pending",
    )
    session.add(txn)
    await session.commit()
    return txn


@pytest.mark.parametrize("source", ["sms", "photo"])
async def test_all_three_actions_and_the_guard_behave_identically_by_source(db_session, source):
    user = await _make_user(db_session)

    linked_expense = await _make_expense(db_session, user, amount="450.00", category="Food", txn_date=date(2026, 8, 10))
    link_txn = await _make_transaction(
        db_session, user, amount="450.00", merchant="Blinkit", txn_date=date(2026, 8, 10), source=source
    )
    log_txn = await _make_transaction(
        db_session,
        user,
        amount="220.00",
        merchant="Zomato",
        txn_date=date(2026, 8, 12),
        source=source,
        category_hint="Food",  # present so photo's auto_log guard doesn't fire — see module docstring
    )
    ask_txn = await _make_transaction(
        db_session, user, amount="999.00", merchant="???", txn_date=date(2026, 8, 12), source=source
    )
    guard_expense = await _make_expense(
        db_session, user, amount="700.00", category="Shopping", txn_date=date(2026, 8, 5)
    )
    guard_txn = await _make_transaction(
        db_session, user, amount="700.00", merchant="BigMart", txn_date=date(2026, 8, 5), source=source
    )

    provider = _ScriptedProvider(
        [
            MatchDecision(
                action="auto_link",
                matched_expense_id=linked_expense.id,
                suggested_category=None,
                confidence=0.95,
                reasoning="Amount and date match exactly.",
            ),
            MatchDecision(
                action="auto_log",
                matched_expense_id=None,
                suggested_category="Food",
                confidence=0.9,
                reasoning="Clear spend, no candidate.",
            ),
            MatchDecision(
                action="ask_user",
                matched_expense_id=None,
                suggested_category=None,
                confidence=0.4,
                reasoning="Merchant is unclear.",
            ),
            MatchDecision(
                action="auto_link",
                matched_expense_id=guard_expense.id,
                suggested_category=None,
                confidence=0.5,  # below settings.confidence_threshold -> guard downgrades this
                reasoning="Looks like a match.",
            ),
        ]
    )

    summary = await reconcile_user(db_session, provider, user.id)

    assert summary.auto_linked == 1
    assert summary.auto_logged == 1
    assert summary.asked_user == 2  # one genuine ask_user + one guard-downgraded auto_link

    await db_session.refresh(link_txn)
    await db_session.refresh(linked_expense)
    assert link_txn.status == "processed"
    assert linked_expense.linked_transaction_id == link_txn.id

    await db_session.refresh(log_txn)
    assert log_txn.status == "processed"
    logged_expense = (await db_session.execute(select(Expense).where(Expense.created_via == "auto_log"))).scalar_one()
    assert logged_expense.category == "Food"
    assert logged_expense.linked_transaction_id == log_txn.id

    await db_session.refresh(ask_txn)
    assert ask_txn.status == "pending"

    await db_session.refresh(guard_txn)
    assert guard_txn.status == "pending"  # guard fired -> stays pending like a real ask_user

    await db_session.refresh(guard_expense)
    assert guard_expense.linked_transaction_id is None  # guard fired before the link happened

    runs = (await db_session.execute(select(ReconciliationRun).order_by(ReconciliationRun.created_at))).scalars().all()
    assert [r.decision for r in runs] == ["auto_link", "auto_log", "ask_user", "ask_user"]
    assert "downgraded to ask_user" in runs[3].reasoning

    # The transaction's own source is untouched by any of this — reconcile_user
    # never reads or branches on it.
    for txn in (link_txn, log_txn, ask_txn, guard_txn):
        assert txn.source == source
