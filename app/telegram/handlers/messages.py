"""Telegram message-ingestion handlers: forwarded SMS text and receipt
photos. No decision logic lives here — parsing/persistence routes through
app.ingestion; app.agent decides what happens to the resulting transaction
later, during /reconcile."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.config import settings
from app.db import SessionLocal
from app.ingestion import ingest_photo_transaction, ingest_sms_transaction
from app.telegram.handlers._shared import _get_or_create_user

logger = logging.getLogger(__name__)


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
        result = await ingest_sms_transaction(session, user, raw_text, provider)

    if result.outcome == "parse_error":
        logger.info("chat=%s: could not parse a transaction from the message", chat_id)
        await update.message.reply_text(
            "I couldn't find a transaction in that message — forward the original bank/UPI "
            "SMS, or if this was cash with no bank alert, log it directly: "
            "/log <category> <amount>, e.g. /log Food 900"
        )
        return

    if result.outcome == "income_recorded":
        source_note = f" from {result.merchant}" if result.merchant else ""
        await update.message.reply_text(
            f"💵 Income of Rs.{result.amount}{source_note} recorded. I'll factor it into your monthly audit."
        )
        return

    await update.message.reply_text(
        f"Got it — Rs.{result.amount} at {result.merchant or 'unknown merchant'} on "
        f"{result.txn_date}. Run /reconcile when you're ready."
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
