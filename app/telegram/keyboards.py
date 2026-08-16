"""Category inline keyboard for the ask_user flow."""

from __future__ import annotations

from uuid import UUID

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.categories import CATEGORIES


def category_keyboard(run_id: UUID) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(label, callback_data=f"cat:{run_id}:{key}") for key, label in CATEGORIES.items()]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)
