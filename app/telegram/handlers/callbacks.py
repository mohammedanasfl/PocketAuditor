"""The ask_user round trip: sending the "which category?" question (once
/reconcile decides ask_user) and handling the inline-button answer."""

from __future__ import annotations

import logging

from telegram import Bot, Update
from telegram.ext import ContextTypes

from app.agent import PendingQuestion
from app.audit import AuditQuestion
from app.categories import CATEGORIES
from app.db import SessionLocal
from app.expenses import recategorize_expense, resolve_ask_user_answer
from app.models import ReconciliationRun
from app.telegram.handlers._shared import get_user_by_chat, parse_uuid
from app.telegram.keyboards import audit_category_keyboard, category_keyboard

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
        # app.agent.act_on_decision just created this run in the same reconcile pass —
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

    run_id = parse_uuid(run_id_str)
    if run_id is None:
        logger.warning("malformed run id in callback_data: %r", query.data)
        return

    chat_id = update.effective_chat.id
    logger.info("chat=%s: category callback run=%s category=%s", chat_id, run_id, category)

    async with SessionLocal() as session:
        user = await get_user_by_chat(session, chat_id)
        expense = None
        if user is not None:
            expense = await resolve_ask_user_answer(session, run_id, category, user_id=user.id)

    if expense is None:
        logger.info("run=%s: already resolved, ignoring callback", run_id_str)
        await query.edit_message_text("This question has already been resolved.")
        return

    await query.edit_message_text(f"Logged as {category}.")


async def send_audit_question(bot: Bot, chat_id: int, question: AuditQuestion) -> None:
    """Send one "recategorize this anomaly?" prompt from the monthly audit,
    with the acat: inline buttons. Unlike send_ask_user_message there's no
    message id to persist — the callback edits its own message in place."""
    logger.info("chat=%s: audit asking to recategorize expense=%s", chat_id, question.expense_id)
    text = (
        f"🔍 Rs.{question.amount:,.2f} at {question.merchant or 'unknown merchant'} — "
        f"currently {question.current_category}, {question.reason}. Recategorize?"
    )
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=audit_category_keyboard(question.expense_id))


async def handle_audit_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the acat: inline-button answer to a monthly-audit anomaly —
    updates the existing expense's category in place."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()

    try:
        _, expense_id_str, key = query.data.split(":", 2)
    except ValueError:
        logger.warning("malformed audit category callback_data: %r", query.data)
        return

    category = CATEGORIES.get(key)
    if category is None:
        logger.warning("unknown category key %r in audit callback_data: %r", key, query.data)
        return

    expense_id = parse_uuid(expense_id_str)
    if expense_id is None:
        logger.warning("malformed expense id in audit callback_data: %r", query.data)
        return

    chat_id = update.effective_chat.id
    logger.info("chat=%s: audit recategorize expense=%s category=%s", chat_id, expense_id, category)

    async with SessionLocal() as session:
        user = await get_user_by_chat(session, chat_id)
        expense = None
        if user is not None:
            expense = await recategorize_expense(session, expense_id, category, user_id=user.id)

    if expense is None:
        await query.edit_message_text("That expense no longer exists.")
        return

    await query.edit_message_text(f"Recategorized as {category}.")
