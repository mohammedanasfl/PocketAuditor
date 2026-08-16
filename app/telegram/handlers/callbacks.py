"""The ask_user round trip: sending the "which category?" question (once
/reconcile decides ask_user) and handling the inline-button answer."""

from __future__ import annotations

import logging
from uuid import UUID

from telegram import Bot, Update
from telegram.ext import ContextTypes

from app.agent import PendingQuestion
from app.categories import CATEGORIES
from app.db import SessionLocal
from app.expenses import resolve_ask_user_answer
from app.models import ReconciliationRun
from app.telegram.keyboards import category_keyboard

logger = logging.getLogger(__name__)


async def send_ask_user_message(bot: Bot, chat_id: int, pending: PendingQuestion) -> None:
    """Send the single "which category?" question with inline buttons, and
    record the resulting message id so handle_category_callback can later
    edit it (strip the buttons) once answered."""
    logger.info("chat=%s: asking user to categorize run=%s", chat_id, pending.run_id)
    text = (
        f"Rs.{pending.transaction.amount} at {pending.transaction.merchant or 'unknown merchant'} "
        f"on {pending.transaction.txn_date} — which category?\n{pending.reasoning}"
    )
    message = await bot.send_message(chat_id=chat_id, text=text, reply_markup=category_keyboard(pending.run_id))

    async with SessionLocal() as session:
        run = await session.get(ReconciliationRun, pending.run_id)
        # app.agent._act just created this run in the same reconcile pass —
        # guaranteed to still resolve here.
        assert run is not None
        run.telegram_message_id = message.message_id
        await session.commit()


async def handle_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the inline-button answer to an ask_user question."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()

    try:
        _, run_id_str, key = query.data.split(":", 2)
    except ValueError:
        logger.warning("malformed category callback_data: %r", query.data)
        return

    category = CATEGORIES.get(key)
    if category is None:
        logger.warning("unknown category key %r in callback_data: %r", key, query.data)
        return

    logger.info("chat=%s: category callback run=%s category=%s", update.effective_chat.id, run_id_str, category)

    async with SessionLocal() as session:
        expense = await resolve_ask_user_answer(session, UUID(run_id_str), category)

    if expense is None:
        logger.info("run=%s: already resolved, ignoring callback", run_id_str)
        await query.edit_message_text("This question has already been resolved.")
        return

    await query.edit_message_text(f"Logged as {category}.")
