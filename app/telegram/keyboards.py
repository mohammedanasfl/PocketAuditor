"""Category inline keyboard for the ask_user flow."""

from __future__ import annotations

from uuid import UUID

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.categories import CATEGORIES


def category_keyboard(run_id: UUID) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(label, callback_data=f"cat:{run_id}:{key}") for key, label in CATEGORIES.items()]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def audit_category_keyboard(expense_id: UUID) -> InlineKeyboardMarkup:
    """Buttons for the monthly audit's "recategorize this anomaly" prompt.
    A distinct `acat:` prefix (vs. reconcile's `cat:`) so the two callback
    handlers never collide — see app/telegram/bot.py's pattern registrations.
    Keyed by expense id, not a run id, because it edits an existing expense."""
    buttons = [
        InlineKeyboardButton(label, callback_data=f"acat:{expense_id}:{key}") for key, label in CATEGORIES.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)
