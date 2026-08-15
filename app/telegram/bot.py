"""Builds the Telegram Application and registers handlers.

Doesn't decide webhook vs polling — that's app.main's job (RUN_MODE).
"""

from __future__ import annotations

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from app.config import settings
from app.llm.factory import get_provider
from app.telegram.handlers import (
    handle_ask_command,
    handle_budgets_command,
    handle_category_callback,
    handle_message,
    handle_photo_message,
    handle_reconcile_command,
    handle_setbudget_command,
    handle_spend_command,
    handle_start_command,
)


def build_application() -> Application:
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["llm_provider"] = get_provider()
    application.add_handler(CommandHandler("start", handle_start_command))
    application.add_handler(CommandHandler("reconcile", handle_reconcile_command))
    application.add_handler(CommandHandler("spend", handle_spend_command))
    application.add_handler(CommandHandler("setbudget", handle_setbudget_command))
    application.add_handler(CommandHandler("budgets", handle_budgets_command))
    application.add_handler(CommandHandler("ask", handle_ask_command))
    application.add_handler(CallbackQueryHandler(handle_category_callback, pattern=r"^cat:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    return application
