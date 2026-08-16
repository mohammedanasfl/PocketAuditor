"""The `/command` handlers. No decision logic lives here — reconciliation
routes through app.agent, expense creation through app.expenses, and
budgets/queries through their own service modules."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.ext import ContextTypes

from app.agent import reconcile_user
from app.budgets import format_budgets_message, get_budget_statuses, upsert_budget
from app.categories import CATEGORIES, normalize_category
from app.db import SessionLocal
from app.expenses import log_manual_expense
from app.llm.base import LLMDecisionError
from app.query import run_query
from app.reports import get_spend_summary
from app.telegram.handlers._shared import _get_or_create_user
from app.telegram.handlers.callbacks import send_ask_user_message

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
    "/log <category> <amount> [notes] — manually log cash or other spend with "
    "no bank alert at all, e.g. /log Food 900 lunch\n"
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


async def handle_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/log <category> <amount> [notes] — manually log an expense with no
    underlying bank/UPI transaction at all (e.g. physical cash spend)."""
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    args = context.args or []
    logger.info("chat=%s: /log %r", chat_id, args)

    usage = "Usage: /log <category> <amount> [notes]\ne.g. /log Food 900 lunch with friends"
    if len(args) < 2:
        await update.message.reply_text(usage)
        return

    # Unlike /setbudget (which keeps asking, since a typo there would
    # silently redirect a monthly limit), an unrecognized /log category just
    # falls back to "Other" — the amount is never lost, and the fallback is
    # called out in the reply so it isn't a silent surprise later.
    category = normalize_category(args[0])
    category_note = ""
    if category is None:
        category_note = f" (unrecognized category {args[0]!r}, defaulted to Other)"
        category = CATEGORIES["other"]

    try:
        amount = Decimal(args[1])
    except InvalidOperation:
        await update.message.reply_text(usage)
        return
    if amount <= 0:
        await update.message.reply_text("Amount must be positive.")
        return

    notes = " ".join(args[2:]) or None

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        await log_manual_expense(session, user.id, category, amount, notes)

    note_suffix = f" ({notes})" if notes else ""
    await update.message.reply_text(f"Logged: Rs.{amount:,.2f} — {category}{note_suffix}{category_note}")


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
        await update.message.reply_text("Usage: /ask <question>\ne.g. /ask how much did I spend on food this week")
        return

    try:
        intent = await provider.interpret_query(question)
    except LLMDecisionError as exc:
        logger.warning("chat=%s: interpret_query failed: %s", chat_id, exc)
        await update.message.reply_text(
            "I couldn't understand that question — try phrasing it like 'how much on food this week?'"
        )
        return

    if not intent.is_expense_question:
        logger.info("chat=%s: /ask question isn't about expenses: %r", chat_id, question)
        await update.message.reply_text(
            "I can only answer questions about your spending — try something like "
            "'how much did I spend on food this week?'"
        )
        return

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        result = await run_query(session, user.id, intent)

    await update.message.reply_text(result.as_message())


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
