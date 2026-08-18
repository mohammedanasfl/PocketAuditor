"""app.merchant_memory — the per-user merchant -> category lookup that lets
app/agent.py:reconcile_user skip asking about a merchant it's already learned.
"""

from __future__ import annotations

from sqlalchemy import select

from app.merchant_memory import normalize_merchant, recall_merchant_category, remember_merchant_category
from app.models import MerchantCategory, User


async def _make_user(session) -> User:
    user = User(telegram_chat_id=54321)
    session.add(user)
    await session.commit()
    return user


def test_normalize_merchant_collapses_whitespace_and_case():
    assert normalize_merchant("  Swiggy   Ltd  ") == "swiggy ltd"
    assert normalize_merchant("SWIGGY") == "swiggy"
    assert normalize_merchant("swiggy") == "swiggy"


async def test_remember_then_recall_round_trip(db_session):
    user = await _make_user(db_session)
    await remember_merchant_category(db_session, user.id, "Swiggy", "Food")
    await db_session.commit()

    assert await recall_merchant_category(db_session, user.id, "swiggy") == "Food"
    assert await recall_merchant_category(db_session, user.id, "  SWIGGY  ") == "Food"


async def test_remember_overwrites_the_existing_category(db_session):
    user = await _make_user(db_session)
    await remember_merchant_category(db_session, user.id, "Swiggy", "Food")
    await db_session.commit()

    await remember_merchant_category(db_session, user.id, "Swiggy", "Entertainment")
    await db_session.commit()

    assert await recall_merchant_category(db_session, user.id, "Swiggy") == "Entertainment"


async def test_remember_is_a_noop_for_no_merchant(db_session):
    user = await _make_user(db_session)
    await remember_merchant_category(db_session, user.id, None, "Food")
    await remember_merchant_category(db_session, user.id, "   ", "Food")
    await db_session.commit()

    rows = (await db_session.execute(select(MerchantCategory))).scalars().all()
    assert rows == []


async def test_recall_returns_none_for_no_merchant_or_unknown_merchant(db_session):
    user = await _make_user(db_session)
    assert await recall_merchant_category(db_session, user.id, None) is None
    assert await recall_merchant_category(db_session, user.id, "Never Seen Before") is None


async def test_recall_is_scoped_per_user(db_session):
    user_a = await _make_user(db_session)
    user_b = User(telegram_chat_id=98765)
    db_session.add(user_b)
    await db_session.commit()

    await remember_merchant_category(db_session, user_a.id, "Swiggy", "Food")
    await db_session.commit()

    assert await recall_merchant_category(db_session, user_b.id, "Swiggy") is None
