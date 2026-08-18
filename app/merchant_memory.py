"""Per-user merchant -> category memory.

app/expenses.py:resolve_ask_user_answer records a merchant's category here
whenever a user answers an ask_user question, so app/agent.py:reconcile_user
never has to ask about the same merchant again once there's nothing else
(no manual expense candidate, no more specific category_hint) to go on.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    key = normalize_merchant(merchant) if merchant else ""
    if not key:
        return None

    row = (
        await session.execute(
            select(MerchantCategory).where(MerchantCategory.user_id == user_id, MerchantCategory.merchant == key)
        )
    ).scalar_one_or_none()
    return row.category if row else None
