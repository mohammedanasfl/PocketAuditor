"""Helpers shared across the handlers package."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

logger = logging.getLogger(__name__)


async def get_user_by_chat(session: AsyncSession, chat_id: int) -> User | None:
    """The user for this Telegram chat, or None if they never used the bot."""
    result = await session.execute(select(User).where(User.telegram_chat_id == chat_id))
    return result.scalar_one_or_none()


def parse_uuid(text: str) -> UUID | None:
    """UUID from button data, or None if it's malformed (never raises)."""
    try:
        return UUID(text)
    except ValueError:
        return None


async def _get_or_create_user(session: AsyncSession, chat_id: int) -> User:
    user = await get_user_by_chat(session, chat_id)
    if user is not None:
        return user
    user = User(telegram_chat_id=chat_id)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        # Two first messages arrived at once and the other one created the
        # user a moment ago — use that row.
        await session.rollback()
        existing = await get_user_by_chat(session, chat_id)
        assert existing is not None
        return existing
    return user
