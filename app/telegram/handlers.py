"""Telegram bot handlers — message ingestion, /reconcile, and the ask_user
category-button callback.

No decision logic lives here; everything routes through app.parser / app.agent.
The configured LLMProvider is shared via application.bot_data["llm_provider"]
(set once in app.telegram.bot.build_application) rather than each handler
constructing its own.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select
from telegram import Bot, Update
from telegram.ext import ContextTypes

from app.agent import PendingQuestion, reconcile_user
from app.budgets import format_budgets_message, get_budget_statuses, upsert_budget
from app.config import settings
from app.db import SessionLocal
from app.llm.base import LLMDecisionError
from app.models import Expense, ReconciliationRun, Transaction, User
from app.parser import ParseError, parse_sms
from app.query import run_query
from app.reports import get_spend_summary
from app.telegram.keyboards import CATEGORIES, category_keyboard, normalize_category

logger = logging.getLogger(__name__)


_WELCOME_TEXT = (
    "👋 Welcome to PocketAuditor!\n\n"
    "I reconcile your bank/UPI SMS alerts against your expense ledger, so you "
    "don't have to check every transaction by hand.\n\n"
    "How it works:\n"
    "1️⃣ Forward me your bank/UPI SMS alerts as they arrive, or send a photo "
    "of a bill/receipt/payment confirmation — I'll parse the amount, "
    "merchant, and date from each one and hold onto it.\n"
    "2️⃣ Run /reconcile whenever you're ready — I'll compare each pending "
    "transaction against the expenses you've already logged.\n"
    "3️⃣ For each one, I'll either:\n"
    "   • auto-match it to an expense you already logged\n"
    "   • log it as a new expense myself, if it's clear-cut\n"
    "   • ask you to pick a category — only when I'm genuinely unsure\n\n"
    "Commands:\n"
    "/start — show this message\n"
    "/reconcile — reconcile your pending transactions\n"
    "/spend — see your spend totals for this week, month, year, and all time\n"
    "/setbudget <category> <amount> — set a monthly limit, e.g. /setbudget Food 4000\n"
    "/budgets — see your limits and this month's spend per category\n"
    "/ask <question> — ask about your spending, e.g. /ask how much on food this week\n\n"
    "Forward your first SMS whenever you're ready."
)


async def handle_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    logger.info("chat=%s: /start", update.effective_chat.id)
    await update.message.reply_text(_WELCOME_TEXT)


async def handle_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    logger.info("chat=%s: /help", update.effective_chat.id)
    await update.message.reply_text(_WELCOME_TEXT)


async def handle_spend_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    logger.info("chat=%s: /spend", chat_id)

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        summary = await get_spend_summary(session, user.id)

    await update.message.reply_text(summary.as_message())


async def handle_setbudget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setbudget <category> <amount> — upserts a monthly limit. Category must
    match one of the CATEGORIES buttons (case-insensitively) so budgets and
    the ask_user flow always share one category vocabulary."""
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    args = context.args or []
    logger.info("chat=%s: /setbudget %r", chat_id, args)

    usage = "Usage: /setbudget <category> <amount>\ne.g. /setbudget Food 4000"
    if len(args) < 2:
        await update.message.reply_text(usage)
        return

    *category_words, amount_str = args
    category = normalize_category(" ".join(category_words))
    if category is None:
        valid = ", ".join(CATEGORIES.values())
        await update.message.reply_text(f"Unknown category — pick one of: {valid}")
        return

    try:
        amount = Decimal(amount_str)
    except InvalidOperation:
        await update.message.reply_text(usage)
        return
    if amount <= 0:
        await update.message.reply_text("Budget amount must be positive.")
        return

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        await upsert_budget(session, user.id, category, amount)

    await update.message.reply_text(f"Budget set: {category} — Rs.{amount:,.2f}/month")


async def handle_budgets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/budgets — lists current limits and this month's spend per category."""
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    logger.info("chat=%s: /budgets", chat_id)

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        statuses = await get_budget_statuses(session, user.id)

    await update.message.reply_text(format_budgets_message(statuses))


async def handle_ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ask <question> — a single, self-contained NL query over the user's
    expense ledger (Phase 3b). Explicit command rather than passive listening
    on every message, same reasoning as the photo handler being opt-in via
    /reconcile rather than auto-triggered: predictable behavior, and it keeps
    a forwarded SMS from ever being misread as a question.

    interpret_query only ever returns a QueryIntent — run_query (app/query.py)
    is the one place that becomes a real, parameterized query; this handler
    never sees or constructs SQL itself.
    """
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    provider = context.bot_data["llm_provider"]
    question = " ".join(context.args or []).strip()
    logger.info("chat=%s: /ask %r", chat_id, question)

    if not question:
        await update.message.reply_text(
            "Usage: /ask <question>\ne.g. /ask how much did I spend on food this week"
        )
        return

    try:
        intent = await provider.interpret_query(question)
    except LLMDecisionError as exc:
        logger.warning("chat=%s: interpret_query failed: %s", chat_id, exc)
        await update.message.reply_text(
            "I couldn't understand that question — try phrasing it like "
            "'how much on food this week?'"
        )
        return

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        result = await run_query(session, user.id, intent)

    await update.message.reply_text(result.as_message())


async def _get_or_create_user(session, chat_id: int) -> User:
    result = await session.execute(select(User).where(User.telegram_chat_id == chat_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(telegram_chat_id=chat_id)
        session.add(user)
        await session.commit()
    return user


async def send_ask_user_message(bot: Bot, chat_id: int, pending: PendingQuestion) -> None:
    """Send the single "which category?" question with inline buttons, and
    record the resulting message id so the callback handler can later edit
    it (strip the buttons) once answered."""
    logger.info("chat=%s: asking user to categorize run=%s", chat_id, pending.run_id)
    text = (
        f"Rs.{pending.transaction.amount} at {pending.transaction.merchant or 'unknown merchant'} "
        f"on {pending.transaction.txn_date} — which category?\n{pending.reasoning}"
    )
    message = await bot.send_message(
        chat_id=chat_id, text=text, reply_markup=category_keyboard(pending.run_id)
    )

    async with SessionLocal() as session:
        run = await session.get(ReconciliationRun, pending.run_id)
        run.telegram_message_id = message.message_id
        await session.commit()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain-text message handler: treat as forwarded SMS/UPI text."""
    if update.message is None or update.message.text is None:
        return
    chat_id = update.effective_chat.id
    raw_text = update.message.text
    provider = context.bot_data["llm_provider"]
    # Don't log the full SMS body — it typically contains partial account
    # numbers and block-UPI phone numbers. A short preview is enough to
    # follow along in the logs.
    logger.info("chat=%s: message received (%d chars): %r", chat_id, len(raw_text), raw_text[:40])

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)

        try:
            parsed = await parse_sms(raw_text, provider)
        except ParseError:
            logger.info("chat=%s: could not parse a transaction from the message", chat_id)
            await update.message.reply_text(
                "I couldn't find a transaction in that message — forward the original bank/UPI SMS."
            )
            return

        if not parsed.is_debit:
            logger.info("chat=%s: skipped credit of Rs.%s (not tracked as an expense)", chat_id, parsed.amount)
            await update.message.reply_text(
                f"Looks like a credit of Rs.{parsed.amount} — not tracked as an expense."
            )
            return

        txn = Transaction(
            user_id=user.id,
            raw_text=raw_text,
            amount=parsed.amount,
            merchant=parsed.merchant,
            txn_date=parsed.txn_date,
            source="sms",
            status="pending",
        )
        session.add(txn)
        await session.commit()
        logger.info(
            "chat=%s: created transaction=%s amount=%s merchant=%r txn_date=%s (parsed via %s, confidence=%.2f)",
            chat_id, txn.id, parsed.amount, parsed.merchant, parsed.txn_date, parsed.method, parsed.confidence,
        )

    await update.message.reply_text(
        f"Got it — Rs.{parsed.amount} at {parsed.merchant or 'unknown merchant'} on "
        f"{parsed.txn_date}. Run /reconcile when you're ready."
    )


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo message handler: treat as a bill/receipt/UPI screenshot. Feeds the
    same `transactions` table and `reconcile_user` loop as SMS text (Phase 2)
    — the only difference is `source='photo'` and how the row gets populated.
    """
    if update.message is None or not update.message.photo:
        return
    chat_id = update.effective_chat.id
    provider = context.bot_data["llm_provider"]
    logger.info("chat=%s: photo received", chat_id)

    # Telegram sends photo sizes smallest-first; take the largest for the best
    # shot at reading small print (amounts, dates) accurately.
    photo = update.message.photo[-1]
    telegram_file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await telegram_file.download_as_bytearray())

    try:
        receipt = await provider.extract_receipt(image_bytes, "image/jpeg")
    except NotImplementedError:
        logger.warning("chat=%s: extract_receipt not implemented for this provider", chat_id)
        await update.message.reply_text(
            "Photo receipts aren't supported by the current LLM setup — type the expense manually for now."
        )
        return
    except LLMDecisionError as exc:
        logger.warning("chat=%s: extract_receipt failed: %s", chat_id, exc)
        await update.message.reply_text(
            "I couldn't read that photo — try retaking it, or type the expense manually."
        )
        return

    if not receipt.readable or receipt.confidence < settings.confidence_threshold or receipt.total_amount is None:
        logger.info(
            "chat=%s: photo receipt not trustworthy (readable=%s confidence=%.2f total_amount=%s)",
            chat_id, receipt.readable, receipt.confidence, receipt.total_amount,
        )
        await update.message.reply_text(
            "That photo wasn't clear enough to read reliably — retake it, or type the expense manually."
        )
        return

    txn_date = receipt.txn_date or date.today()
    # A caption is the user explicitly telling us the category up front (e.g.
    # sending a receipt photo with "Food" as the caption) — trust it over
    # letting the agent loop guess one later. normalize_category rejects
    # anything that isn't one of the known categories rather than silently
    # accepting a typo as a brand-new one.
    category_hint = normalize_category(update.message.caption) if update.message.caption else None
    raw_text = (
        f"[Photo receipt] merchant={receipt.merchant!r} "
        f"line_items={receipt.line_items or []}"
    )

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        txn = Transaction(
            user_id=user.id,
            raw_text=raw_text,
            amount=Decimal(str(receipt.total_amount)),
            merchant=receipt.merchant,
            txn_date=txn_date,
            source="photo",
            category_hint=category_hint,
            status="pending",
        )
        session.add(txn)
        await session.commit()
        logger.info(
            "chat=%s: created transaction=%s amount=%s merchant=%r txn_date=%s category_hint=%r (source=photo, confidence=%.2f)",
            chat_id, txn.id, receipt.total_amount, receipt.merchant, txn_date, category_hint, receipt.confidence,
        )

    category_note = f" — categorized as {category_hint}" if category_hint else ""
    await update.message.reply_text(
        f"Got it — Rs.{receipt.total_amount} at {receipt.merchant or 'unknown merchant'} on "
        f"{txn_date}{category_note}. Run /reconcile when you're ready."
    )


async def handle_reconcile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reconcile — runs the agent loop for the requesting chat's user only."""
    chat_id = update.effective_chat.id
    provider = context.bot_data["llm_provider"]
    logger.info("chat=%s: /reconcile requested", chat_id)

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        summary = await reconcile_user(session, provider, user.id)

    logger.info("chat=%s: reconcile finished — %s", chat_id, summary.as_message())

    for pending in summary.pending_questions:
        await send_ask_user_message(context.bot, chat_id, pending)

    await context.bot.send_message(chat_id=chat_id, text=summary.as_message())


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
        run = await session.get(ReconciliationRun, UUID(run_id_str))
        if run is None or run.status != "open":
            logger.info("run=%s: already resolved, ignoring callback", run_id_str)
            await query.edit_message_text("This question has already been resolved.")
            return

        txn = await session.get(Transaction, run.transaction_id)

        expense = Expense(
            user_id=run.user_id,
            amount=txn.amount,
            category=category,
            merchant=txn.merchant,
            txn_date=txn.txn_date,
            linked_transaction_id=txn.id,
            created_via="manual",
        )
        session.add(expense)

        txn.status = "processed"
        run.status = "resolved"
        run.resolved_at = datetime.now(timezone.utc)

        await session.commit()

    await query.edit_message_text(f"Logged as {category}.")
