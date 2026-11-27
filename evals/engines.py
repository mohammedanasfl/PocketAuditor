"""The ways a forwarded SMS can be processed. The eval runner runs the same
scenarios through each one so they can be compared.

- pipeline: the original fixed flow — parse, save, then reconcile_user().
- agent:    parse, save, then the tool-using agent (app/agent_core/).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import reconcile_user
from app.agent_core.reconcile import reconcile_with_agent
from app.ingestion import ingest_sms_transaction
from app.llm.base import LLMProvider
from app.models import User


async def pipeline_engine(session: AsyncSession, user: User, sms: str, provider: LLMProvider) -> None:
    result = await ingest_sms_transaction(session, user, sms, provider)
    if result.outcome == "transaction_created":
        await reconcile_user(session, provider, user.id)


async def agent_engine(session: AsyncSession, user: User, sms: str, provider: LLMProvider) -> None:
    result = await ingest_sms_transaction(session, user, sms, provider)
    if result.outcome == "transaction_created":
        await reconcile_with_agent(session, provider, user.id)


ENGINES: dict[str, Callable[[AsyncSession, User, str, LLMProvider], Awaitable[None]]] = {
    "pipeline": pipeline_engine,
    "agent": agent_engine,
}
