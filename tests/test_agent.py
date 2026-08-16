"""Stage 4 tests: the perceive -> compare -> decide -> act loop, run against
real DB writes (aiosqlite) with a scripted FakeProvider — covers all three
act-branches, both code-level conservatism guards, per-transaction error
isolation, and candidate exclusion.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.agent import reconcile_user
from app.llm.base import LLMDecisionError
from app.models import Expense, ReconciliationRun, Transaction, User
from app.schemas import MatchDecision


class _ScriptedProvider:
    """Returns pre-built MatchDecision objects in call order."""

    def __init__(self, decisions: list[MatchDecision]):
        self._decisions = list(decisions)
        self.calls: list[tuple[dict, list[dict]]] = []

    async def decide_match(self, transaction: dict, candidates: list[dict]) -> MatchDecision:
        self.calls.append((transaction, candidates))
        return self._decisions.pop(0)

    async def parse_transaction(self, raw_text: str):  # pragma: no cover
        raise AssertionError("not used by the agent loop")


async def _make_user(session) -> User:
    user = User(telegram_chat_id=12345)
    session.add(user)
    await session.commit()
    return user


async def _make_transaction(
    session, user: User, *, amount: str, merchant: str, txn_date: date,
    source: str = "sms", category_hint: str | None = None,
) -> Transaction:
    txn = Transaction(
        user_id=user.id,
        raw_text=f"Rs.{amount} debited towards {merchant}",
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


async def _make_expense(session, user: User, *, amount: str, category: str, txn_date: date) -> Expense:
    expense = Expense(user_id=user.id, amount=Decimal(amount), category=category, txn_date=txn_date)
    session.add(expense)
    await session.commit()
    return expense


# --- auto_link ---------------------------------------------------------


async def test_auto_link_links_expense_and_marks_transaction_processed(db_session):
    user = await _make_user(db_session)
    expense = await _make_expense(
        db_session, user, amount="450.00", category="Food", txn_date=date(2026, 8, 10)
    )
    txn = await _make_transaction(
        db_session, user, amount="450.00", merchant="Blinkit", txn_date=date(2026, 8, 10)
    )

    provider = _ScriptedProvider(
        [
            MatchDecision(
                action="auto_link",
                matched_expense_id=expense.id,
                suggested_category=None,
                confidence=0.95,
                reasoning="Amount and date match exactly.",
            )
        ]
    )

    summary = await reconcile_user(db_session, provider, user.id)

    assert summary.auto_linked == 1
    assert summary.total == 1

    await db_session.refresh(txn)
    await db_session.refresh(expense)
    assert txn.status == "processed"
    assert expense.linked_transaction_id == txn.id

    run = (await db_session.execute(select(ReconciliationRun))).scalar_one()
    assert run.decision == "auto_link"
    assert run.status == "resolved"
    assert run.resolved_at is not None


# --- auto_log ------------------------------------------------------------


async def test_auto_log_creates_new_expense(db_session):
    user = await _make_user(db_session)
    txn = await _make_transaction(
        db_session, user, amount="220.00", merchant="Zomato", txn_date=date(2026, 8, 12)
    )

    provider = _ScriptedProvider(
        [
            MatchDecision(
                action="auto_log",
                matched_expense_id=None,
                suggested_category="Food",
                confidence=0.9,
                reasoning="No candidate, but a clear food-delivery spend.",
            )
        ]
    )

    summary = await reconcile_user(db_session, provider, user.id)

    assert summary.auto_logged == 1
    await db_session.refresh(txn)
    assert txn.status == "processed"

    expense = (await db_session.execute(select(Expense))).scalar_one()
    assert expense.category == "Food"
    assert expense.created_via == "auto_log"
    assert expense.linked_transaction_id == txn.id


async def test_auto_log_falls_back_to_uncategorized_when_no_category_suggested(db_session):
    user = await _make_user(db_session)
    await _make_transaction(
        db_session, user, amount="50.00", merchant="Unknown Shop", txn_date=date(2026, 8, 12)
    )

    provider = _ScriptedProvider(
        [
            MatchDecision(
                action="auto_log",
                matched_expense_id=None,
                suggested_category=None,
                confidence=0.9,
                reasoning="Clear amount and merchant.",
            )
        ]
    )
    await reconcile_user(db_session, provider, user.id)

    expense = (await db_session.execute(select(Expense))).scalar_one()
    assert expense.category == "Uncategorized"


# --- ask_user ----------------------------------------------------------


async def test_ask_user_leaves_transaction_pending_and_opens_run(db_session):
    user = await _make_user(db_session)
    txn = await _make_transaction(
        db_session, user, amount="999.00", merchant="???", txn_date=date(2026, 8, 12)
    )

    provider = _ScriptedProvider(
        [
            MatchDecision(
                action="ask_user",
                matched_expense_id=None,
                suggested_category=None,
                confidence=0.4,
                reasoning="Merchant is unclear and no candidates match.",
            )
        ]
    )

    summary = await reconcile_user(db_session, provider, user.id)

    assert summary.asked_user == 1
    assert len(summary.pending_questions) == 1
    assert summary.pending_questions[0].transaction.id == txn.id

    await db_session.refresh(txn)
    assert txn.status == "pending"  # not processed — waiting on the user

    run = (await db_session.execute(select(ReconciliationRun))).scalar_one()
    assert run.status == "open"
    assert run.resolved_at is None


# --- guard: low confidence downgraded to ask_user ---------------------------


async def test_low_confidence_auto_link_is_downgraded_to_ask_user(db_session):
    user = await _make_user(db_session)
    expense = await _make_expense(
        db_session, user, amount="450.00", category="Food", txn_date=date(2026, 8, 10)
    )
    await _make_transaction(
        db_session, user, amount="450.00", merchant="Blinkit", txn_date=date(2026, 8, 10)
    )

    provider = _ScriptedProvider(
        [
            MatchDecision(
                action="auto_link",
                matched_expense_id=expense.id,
                suggested_category=None,
                confidence=0.5,  # below the 0.75 threshold
                reasoning="Looks like a match.",
            )
        ]
    )

    summary = await reconcile_user(db_session, provider, user.id)

    assert summary.auto_linked == 0
    assert summary.asked_user == 1

    run = (await db_session.execute(select(ReconciliationRun))).scalar_one()
    assert run.decision == "ask_user"
    assert "downgraded to ask_user" in run.reasoning

    await db_session.refresh(expense)
    assert expense.linked_transaction_id is None  # guard fired before the link happened


# --- guard: hallucinated matched_expense_id downgraded to ask_user ---------


async def test_hallucinated_matched_expense_id_is_downgraded_to_ask_user(db_session):
    user = await _make_user(db_session)
    await _make_expense(db_session, user, amount="450.00", category="Food", txn_date=date(2026, 8, 10))
    await _make_transaction(
        db_session, user, amount="450.00", merchant="Blinkit", txn_date=date(2026, 8, 10)
    )

    fake_id = uuid.uuid4()  # not among the candidates offered
    provider = _ScriptedProvider(
        [
            MatchDecision(
                action="auto_link",
                matched_expense_id=fake_id,
                suggested_category=None,
                confidence=0.95,
                reasoning="Confident match.",
            )
        ]
    )

    summary = await reconcile_user(db_session, provider, user.id)

    assert summary.auto_linked == 0
    assert summary.asked_user == 1

    run = (await db_session.execute(select(ReconciliationRun))).scalar_one()
    assert "not among the candidates" in run.reasoning


# --- guard: photo auto_log with no category_hint asks instead of guessing --


async def test_photo_auto_log_with_no_category_hint_is_downgraded_to_ask_user(db_session):
    user = await _make_user(db_session)
    await _make_transaction(
        db_session, user, amount="150.00", merchant="Surendra Kumar Kachurimal",
        txn_date=date(2026, 8, 9), source="photo",  # no category_hint — no caption was given
    )

    provider = _ScriptedProvider(
        [
            MatchDecision(
                action="auto_log", matched_expense_id=None, suggested_category="Other",
                confidence=0.9, reasoning="Clear amount, unclear category.",
            )
        ]
    )

    summary = await reconcile_user(db_session, provider, user.id)

    assert summary.auto_logged == 0
    assert summary.asked_user == 1
    run = (await db_session.execute(select(ReconciliationRun))).scalar_one()
    assert run.decision == "ask_user"
    assert "no category hint" in run.reasoning


async def test_sms_auto_log_with_no_category_hint_is_unaffected(db_session):
    """The new guard is specific to photo transactions — SMS has no caption
    concept at all, so it must keep auto-logging exactly as before."""
    user = await _make_user(db_session)
    await _make_transaction(
        db_session, user, amount="220.00", merchant="Zomato", txn_date=date(2026, 8, 12), source="sms",
    )

    provider = _ScriptedProvider(
        [
            MatchDecision(
                action="auto_log", matched_expense_id=None, suggested_category="Food",
                confidence=0.9, reasoning="Clear food-delivery spend.",
            )
        ]
    )

    summary = await reconcile_user(db_session, provider, user.id)
    assert summary.auto_logged == 1


# --- category_hint overrides the model's own suggested_category -----------


async def test_photo_category_hint_overrides_suggested_category(db_session):
    """A caption is the user's explicit choice — even if the model suggests
    a different category, the hint wins."""
    user = await _make_user(db_session)
    await _make_transaction(
        db_session, user, amount="450.00", merchant="Reliance Fresh", txn_date=date(2026, 8, 14),
        source="photo", category_hint="Food",
    )

    provider = _ScriptedProvider(
        [
            MatchDecision(
                action="auto_log", matched_expense_id=None, suggested_category="Shopping",
                confidence=0.9, reasoning="Looks like a retail purchase.",
            )
        ]
    )

    summary = await reconcile_user(db_session, provider, user.id)

    assert summary.auto_logged == 1  # category_hint present -> guard doesn't fire
    expense = (await db_session.execute(select(Expense))).scalar_one()
    assert expense.category == "Food"  # hint wins over the model's "Shopping" guess


# --- per-transaction LLM error doesn't abort the batch ---------------------


async def test_llm_error_on_one_transaction_does_not_abort_the_batch(db_session):
    class _FlakyProvider(_ScriptedProvider):
        async def decide_match(self, transaction, candidates):
            if len(self.calls) == 0:
                self.calls.append((transaction, candidates))
                raise LLMDecisionError("simulated provider failure")
            return await super().decide_match(transaction, candidates)

    user = await _make_user(db_session)
    await _make_transaction(db_session, user, amount="10.00", merchant="A", txn_date=date(2026, 8, 1))
    await _make_transaction(db_session, user, amount="20.00", merchant="B", txn_date=date(2026, 8, 2))

    provider = _FlakyProvider(
        [
            MatchDecision(
                action="auto_log",
                matched_expense_id=None,
                suggested_category="Other",
                confidence=0.9,
                reasoning="Clear enough.",
            )
        ]
    )

    summary = await reconcile_user(db_session, provider, user.id)

    assert summary.errors == 1
    assert summary.auto_logged == 1
    assert summary.total == 2


# --- candidate exclusion: already-linked expenses are never offered -------


async def test_already_linked_expense_is_not_offered_as_a_candidate(db_session):
    user = await _make_user(db_session)
    expense = await _make_expense(
        db_session, user, amount="450.00", category="Food", txn_date=date(2026, 8, 10)
    )
    other_txn = await _make_transaction(
        db_session, user, amount="450.00", merchant="Blinkit", txn_date=date(2026, 8, 10)
    )
    other_txn.status = "processed"  # simulate: already reconciled in a prior run
    expense.linked_transaction_id = other_txn.id
    await db_session.commit()

    await _make_transaction(
        db_session, user, amount="451.00", merchant="Blinkit", txn_date=date(2026, 8, 10)
    )

    provider = _ScriptedProvider(
        [
            MatchDecision(
                action="auto_log",
                matched_expense_id=None,
                suggested_category="Food",
                confidence=0.9,
                reasoning="No unlinked candidates, but a clear spend.",
            )
        ]
    )

    await reconcile_user(db_session, provider, user.id)

    for _, candidates in provider.calls:
        assert all(c["id"] != str(expense.id) for c in candidates)


# --- summary message formatting ---------------------------------------------


async def test_summary_message_matches_brief_format(db_session):
    user = await _make_user(db_session)
    await _make_transaction(db_session, user, amount="1.00", merchant="A", txn_date=date(2026, 8, 1))
    await _make_transaction(db_session, user, amount="2.00", merchant="B", txn_date=date(2026, 8, 2))
    await _make_transaction(db_session, user, amount="3.00", merchant="C", txn_date=date(2026, 8, 3))

    provider = _ScriptedProvider(
        [
            MatchDecision(action="auto_log", matched_expense_id=None, suggested_category="Other", confidence=0.9, reasoning="x"),
            MatchDecision(action="auto_log", matched_expense_id=None, suggested_category="Other", confidence=0.9, reasoning="x"),
            MatchDecision(action="ask_user", matched_expense_id=None, suggested_category=None, confidence=0.4, reasoning="x"),
        ]
    )

    summary = await reconcile_user(db_session, provider, user.id)
    assert summary.as_message() == "0 matched, 2 auto-logged, 1 needs your input"


async def test_summary_message_when_nothing_pending(db_session):
    user = await _make_user(db_session)
    summary = await reconcile_user(db_session, _ScriptedProvider([]), user.id)
    assert summary.total == 0
    assert summary.as_message() == "Nothing to reconcile — you're all caught up."
