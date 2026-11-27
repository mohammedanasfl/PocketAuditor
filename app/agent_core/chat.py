"""Answer a free-form chat message with the agent.

    "how much did I spend on food this week?"
    "set my shopping budget to 3000"
    "how much money do I have left?"

Read tools answer questions; write tools become Yes/No buttons
(PendingActions). The last few messages are included so follow-ups like
"and last week?" work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_core.chat_tools import CHAT_TOOLS
from app.agent_core.loop import run_agent
from app.agent_core.prompts import chat_system
from app.agent_core.tools import ToolContext
from app.llm.base import LLMProvider
from app.models import ConversationMessage, PendingAction
from app.timeutil import local_today

logger = logging.getLogger(__name__)

# How many earlier messages the agent sees.
_HISTORY_MESSAGES = 10


@dataclass
class ChatReply:
    text: str
    confirmations: list[PendingAction] = field(default_factory=list)


async def _recent_history(session: AsyncSession, user_id: UUID) -> str:
    rows = (
        (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.user_id == user_id)
                .order_by(ConversationMessage.created_at.desc())
                .limit(_HISTORY_MESSAGES)
            )
        )
        .scalars()
        .all()
    )
    lines = []
    for row in reversed(rows):  # oldest first
        speaker = "User" if row.role == "user" else "You"
        lines.append(f"{speaker}: {row.text}")
    return "\n".join(lines)


async def answer_chat(session: AsyncSession, provider: LLMProvider, user_id: UUID, text: str) -> ChatReply:
    history = await _recent_history(session, user_id)
    goal = f"Earlier conversation:\n{history}\n\nNew message from the user:\n{text}" if history else text

    ctx = ToolContext(session=session, user_id=user_id)
    result = await run_agent(
        session=session,
        provider=provider,
        ctx=ctx,
        trigger="chat",
        system=chat_system(local_today().isoformat()),
        goal=goal,
        tools=CHAT_TOOLS,
    )

    if result.status == "over_daily_limit":
        reply = "You've reached today's limit for questions. Try again tomorrow."
    elif result.status == "failed":
        reply = "Sorry, I couldn't answer that right now. Please try again in a moment."
    elif result.text:
        reply = result.text
    elif ctx.confirmations:
        reply = "Please confirm below."
    else:
        reply = "Sorry, I'm not sure how to help with that."

    # Explicit timestamps so the reply always sorts after the question.
    now = datetime.now(UTC)
    session.add(ConversationMessage(user_id=user_id, role="user", text=text, created_at=now))
    session.add(
        ConversationMessage(user_id=user_id, role="assistant", text=reply, created_at=now + timedelta(milliseconds=1))
    )
    await session.commit()
    return ChatReply(text=reply, confirmations=ctx.confirmations)
