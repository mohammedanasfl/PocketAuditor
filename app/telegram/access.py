"""Who is allowed to use the bot.

Runs before every other handler. A chat that isn't on ALLOWED_CHAT_IDS gets
one short reply (with its chat id, so the owner can add it) and nothing else
happens — no database row, no LLM call.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from app.config import settings

logger = logging.getLogger(__name__)


def is_chat_allowed(chat_id: int, allowed_setting: str) -> bool:
    """allowed_setting is "*" (anyone), "" (nobody), or "123,456"."""
    allowed_setting = allowed_setting.strip()
    if allowed_setting == "*":
        return True
    allowed_ids = set()
    for part in allowed_setting.split(","):
        part = part.strip()
        if part:
            allowed_ids.add(part)
    return str(chat_id) in allowed_ids


async def block_unknown_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or is_chat_allowed(chat.id, settings.allowed_chat_ids):
        return  # allowed — let the normal handlers run

    logger.warning("chat=%s: blocked (not in ALLOWED_CHAT_IDS)", chat.id)
    if update.message is not None:
        await update.message.reply_text(
            f"This bot is private. Your chat id is {chat.id} — ask the owner to add it to ALLOWED_CHAT_IDS."
        )
    raise ApplicationHandlerStop  # stop here: no other handler runs


async def log_handler_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last-resort error handler: log the error, and tell the user something
    went wrong instead of leaving them with no reply."""
    logger.error("handler error for update %s", getattr(update, "update_id", "?"), exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text("Sorry, something went wrong. Please try again.")
        except Exception:
            logger.exception("could not send the error reply")
