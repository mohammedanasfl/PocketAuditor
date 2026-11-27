"""Telegram message handlers: forwarded SMS text, chat messages, and receipt
photos. No decision logic lives here — parsing/persistence routes through
app.ingestion, decisions through app.agent_core (reconcile + chat)."""

from __future__ import annotations

import logging
from decimal import Decimal

from telegram import Update
from telegram.ext import ContextTypes

from app.agent_core.chat import answer_chat
from app.agent_core.reconcile import reconcile
from app.config import settings
from app.db import SessionLocal
from app.ingestion import ingest_photo_transaction, ingest_sms_transaction
from app.month_plan import MonthPlan, check_money_alerts, get_month_plan
from app.parser import looks_like_bank_sms
from app.telegram.handlers._shared import _get_or_create_user
from app.telegram.handlers.callbacks import send_ask_user_message, send_confirmation

logger = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain-text message: a forwarded bank SMS, or a chat message for the agent."""
    if update.message is None or update.message.text is None:
        return
    text = update.message.text
    if looks_like_bank_sms(text):
        await _handle_sms(update, context, text)
    else:
        await answer_chat_message(update, context, text)


async def answer_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Free-form chat → the agent. Proposed changes come back as Yes/No buttons."""
    assert update.message is not None
    chat_id = update.effective_chat.id
    provider = context.bot_data["llm_provider"]
    logger.info("chat=%s: chat message (%d chars)", chat_id, len(text))

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        reply = await answer_chat(session, provider, user.id, text)

    await update.message.reply_text(reply.text)
    for action in reply.confirmations:
        await send_confirmation(context.bot, chat_id, action)


async def _handle_sms(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> None:
    """Save the SMS, process it right away, reply with what happened."""
    assert update.message is not None
    chat_id = update.effective_chat.id
    provider = context.bot_data["llm_provider"]
    # Don't log the full SMS body — it typically contains partial account
    # numbers and block-UPI phone numbers. A short preview is enough to
    # follow along in the logs.
    logger.info("chat=%s: SMS received (%d chars): %r", chat_id, len(raw_text), raw_text[:40])

    summary = None
    plan = None
    alerts = []
    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        result = await ingest_sms_transaction(session, user, raw_text, provider)
        if result.outcome == "transaction_created":
            # Decide it now (agent or pipeline) instead of waiting for /reconcile.
            summary = await reconcile(session, provider, user.id, trigger="sms")
        if result.outcome not in ("parse_error", "already_seen", "savings_side_skipped"):
            plan = await get_month_plan(session, user.id)
            alerts = await check_money_alerts(session, user.id)

    if result.outcome == "parse_error":
        logger.info("chat=%s: could not parse a transaction from the message", chat_id)
        await update.message.reply_text(
            "I couldn't find a transaction in that message — forward the original bank/UPI "
            "SMS, or if this was cash with no bank alert, log it directly: "
            "/log <category> <amount>, e.g. /log Food 900"
        )
        return

    if result.outcome == "savings_side_skipped":
        await update.message.reply_text(
            "That's your savings account's side of a transfer. I count transfers from your main (salary) "
            "account's SMS, so I skipped this one to avoid counting it twice."
        )
        return

    if result.outcome == "already_seen":
        await update.message.reply_text("I already have this SMS saved — skipping it so it isn't counted twice.")
        return

    assert plan is not None
    if result.outcome == "income_recorded":
        await update.message.reply_text(_income_reply(result.amount, result.merchant, result.income_kind, plan))
    elif result.outcome == "savings_transfer":
        await update.message.reply_text(_savings_transfer_reply(result.amount, plan))
    elif result.outcome == "savings_withdrawal":
        await update.message.reply_text(_savings_withdrawal_reply(result.amount, plan))
    else:
        assert summary is not None
        for pending in summary.pending_questions:
            await send_ask_user_message(context.bot, chat_id, pending)
        await update.message.reply_text(
            f"Got it — Rs.{result.amount} at {result.merchant or 'unknown merchant'} on {result.txn_date}. "
            f"{summary.as_message()}"
        )

    for alert in alerts:
        await context.bot.send_message(chat_id=chat_id, text=alert.as_message())


def _pct_note(plan: MonthPlan) -> str:
    return f" ({plan.saved_pct}% of this month's salary)" if plan.saved_pct is not None else ""


def _income_reply(amount: Decimal | None, source: str | None, kind: str | None, plan: MonthPlan) -> str:
    if kind == "salary":
        return (
            f"💵 Salary received: Rs.{amount:,.2f}. When you move money to savings, forward that SMS too.\n"
            f"Spending money this month: Rs.{plan.spending_money:,.2f}."
        )
    if kind == "refund":
        return f"↩️ Refund of Rs.{amount:,.2f} recorded."
    source_note = f" from {source}" if source else ""
    return f"💵 Income of Rs.{amount:,.2f}{source_note} recorded. I'll factor it into your monthly audit."


def _savings_transfer_reply(amount: Decimal | None, plan: MonthPlan) -> str:
    return (
        f"🏦 Rs.{amount:,.2f} moved to savings ✅ — saved this month: Rs.{plan.saved:,.2f}{_pct_note(plan)}.\n"
        f"Spending money now: Rs.{plan.spending_money:,.2f} (Rs.{plan.money_left:,.2f} left)."
    )


def _savings_withdrawal_reply(amount: Decimal | None, plan: MonthPlan) -> str:
    return (
        f"🏦 Rs.{amount:,.2f} taken back from savings (not counted as income).\n"
        f"Saved this month: Rs.{plan.saved:,.2f}{_pct_note(plan)}. Money left to spend: Rs.{plan.money_left:,.2f}."
    )


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo message handler: treat as a bill/receipt/UPI screenshot. Feeds
    the same `transactions` table and `reconcile_user` loop as SMS text —
    the only difference is `source='photo'` and how the row gets populated.
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

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        result = await ingest_photo_transaction(
            session, user, image_bytes, update.message.caption, provider, settings.confidence_threshold
        )

    if result.outcome == "not_implemented":
        logger.warning("chat=%s: extract_receipt not implemented for this provider", chat_id)
        await update.message.reply_text(
            "Photo receipts aren't supported by the current LLM setup — "
            "log it yourself instead: /log <category> <amount>"
        )
        return

    if result.outcome == "llm_error":
        await update.message.reply_text(
            "I couldn't read that photo — try retaking it, or log it yourself: /log <category> <amount>"
        )
        return

    if result.outcome == "low_confidence":
        await update.message.reply_text(
            "That photo wasn't clear enough to read reliably — retake it, or log it yourself: /log <category> <amount>"
        )
        return

    category_note = f" — categorized as {result.category_hint}" if result.category_hint else ""
    await update.message.reply_text(
        f"Got it — Rs.{result.amount} at {result.merchant or 'unknown merchant'} on "
        f"{result.txn_date}{category_note}. Run /reconcile when you're ready."
    )
