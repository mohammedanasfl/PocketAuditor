"""Per-user merchant -> category memory.

app/expenses.py:resolve_ask_user_answer records a merchant's category here
whenever a user answers an ask_user question, so app/agent.py:reconcile_user
never has to ask about the same merchant again once there's nothing else
(no manual expense candidate, no more specific category_hint) to go on.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MerchantCategory


def normalize_merchant(raw: str) -> str:
    """Collapses whitespace and case so the same merchant recorded via
    different SMS/photo phrasing still hits the same memory row."""
    return " ".join(raw.split()).lower()


async def remember_merchant_category(session: AsyncSession, user_id: UUID, merchant: str | None, category: str) -> None:
    """No-op when there's no usable merchant string. Does not commit — the
    caller commits, so this stays atomic with whatever else it writes."""
    key = normalize_merchant(merchant) if merchant else ""
    if not key:
        return

    existing = (
        await session.execute(
            select(MerchantCategory).where(MerchantCategory.user_id == user_id, MerchantCategory.merchant == key)
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.category = category
        existing.updated_at = datetime.now(UTC)
    else:
        session.add(MerchantCategory(user_id=user_id, merchant=key, category=category))


async def recall_merchant_category(session: AsyncSession, user_id: UUID, merchant: str | None) -> str | None:
    """The category the user chose for this merchant before, or None.

    Exact match first. Otherwise a fuzzy match, so "ZOMATO LTD",
    "zomato@hdfc" and "Zomato" are the same merchant — but only when every
    similar remembered merchant agrees on one category (two different
    answers means we can't be sure, so return None and let the agent/user
    decide)."""
    key = normalize_merchant(merchant) if merchant else ""
    if not key:
        return None

    rows = (await session.execute(select(MerchantCategory).where(MerchantCategory.user_id == user_id))).scalars().all()
    for row in rows:
        if row.merchant == key:
            return row.category

    similar_categories = {row.category for row in rows if similar_merchants(key, row.merchant)}
    if len(similar_categories) == 1:
        return similar_categories.pop()
    return None


# Words that don't help tell merchants apart.
_FILLER_WORDS = {"ltd", "pvt", "private", "limited", "india", "the", "co", "llp", "inc", "upi", "vpa"}
# How alike two merchant names must be (0..1) to count as the same.
_SIMILARITY_NEEDED = 0.85


def merchant_words(raw: str) -> set[str]:
    """The meaningful words of a merchant name.
    "blinkit@ybl BLINKIT" → {"blinkit"};  "ZOMATO LTD" → {"zomato"}."""
    text = re.sub(r"@\w+", " ", raw.lower())  # drop the UPI handle (@ybl, @hdfc ...)
    words = re.split(r"[^a-z0-9]+", text)
    return {word for word in words if len(word) >= 2 and word not in _FILLER_WORDS}


def similar_merchants(a: str, b: str) -> bool:
    """True if two merchant names very likely mean the same merchant."""
    words_a = merchant_words(a)
    words_b = merchant_words(b)
    if not words_a or not words_b:
        return False
    if words_a == words_b:
        return True
    # One name is the other plus extra words ("amazon" vs "amazon india pay") —
    # only when the shared part is a real word, not a 2-3 letter fragment.
    smaller = words_a if len(words_a) <= len(words_b) else words_b
    larger = words_b if smaller is words_a else words_a
    if smaller <= larger and sum(len(word) for word in smaller) >= 5:
        return True
    # Small spelling differences ("swigy" vs "swiggy").
    joined_a = " ".join(sorted(words_a))
    joined_b = " ".join(sorted(words_b))
    return SequenceMatcher(None, joined_a, joined_b).ratio() >= _SIMILARITY_NEEDED
