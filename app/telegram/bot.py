"""Builds the Telegram Application and registers handlers.

Doesn't decide webhook vs polling — that's app.main's job (RUN_MODE).
"""

from __future__ import annotations

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from app.config import settings
from app.llm.factory import get_provider
from app.telegram.handlers import (
    handle_ask_command,
    handle_budgets_command,
    handle_category_callback,
    handle_help_command,
    handle_message,
    handle_photo_message,
    handle_reconcile_command,
    handle_setbudget_command,
    handle_spend_command,
    handle_start_command,
)

# Shown in Telegram's native "/" command menu — kept here next to the
# CommandHandler registrations below so the two can't silently drift apart.
BOT_COMMANDS = [
    BotCommand("start", "Show the welcome message"),
    BotCommand("help", "Show the welcome message and command list"),
    BotCommand("reconcile", "Reconcile your pending transactions"),
    BotCommand("spend", "See your spend totals (week/month/year/all-time)"),
    BotCommand("setbudget", "Set a monthly limit, e.g. /setbudget Food 4000"),
    BotCommand("budgets", "See your limits and this month's spend per category"),
    BotCommand("ask", "Ask about your spending, e.g. /ask how much on food this week"),
]


def build_application() -> Application:
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["llm_provider"] = get_provider()
    application.add_handler(CommandHandler("start", handle_start_command))
    application.add_handler(CommandHandler("help", handle_help_command))
    application.add_handler(CommandHandler("reconcile", handle_reconcile_command))
    application.add_handler(CommandHandler("spend", handle_spend_command))
    application.add_handler(CommandHandler("setbudget", handle_setbudget_command))
    application.add_handler(CommandHandler("budgets", handle_budgets_command))
    application.add_handler(CommandHandler("ask", handle_ask_command))
    application.add_handler(CallbackQueryHandler(handle_category_callback, pattern=r"^cat:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    return application


async def set_bot_commands(application: Application) -> None:
    """Registers BOT_COMMANDS with Telegram so they appear in the client's
    native "/" menu. Must run after application.initialize() — needs a live
    bot connection, unlike build_application() which is just wiring."""
    await application.bot.set_my_commands(BOT_COMMANDS)
