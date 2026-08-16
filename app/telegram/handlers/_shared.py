"""Helpers shared across the handlers package."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

logger = logging.getLogger(__name__)


async def _get_or_create_user(session: AsyncSession, chat_id: int) -> User:
    result = await session.execute(select(User).where(User.telegram_chat_id == chat_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(telegram_chat_id=chat_id)
        session.add(user)
        await session.commit()
    return user
