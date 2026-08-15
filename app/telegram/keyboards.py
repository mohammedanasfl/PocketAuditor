"""Category inline keyboard for the ask_user flow."""

from __future__ import annotations

from uuid import UUID

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Short keys keep callback_data ("cat:<run_id>:<key>") well under Telegram's
# 64-byte cap even with a full 36-char UUID.
CATEGORIES: dict[str, str] = {
    "food": "Food",
    "transport": "Transport",
    "shopping": "Shopping",
    "bills": "Bills",
    "health": "Health",
    "entertainment": "Entertainment",
    "other": "Other",
}


def category_keyboard(run_id: UUID) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(label, callback_data=f"cat:{run_id}:{key}")
        for key, label in CATEGORIES.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def normalize_category(raw: str) -> str | None:
    """Case-insensitively match free-text input (e.g. from /setbudget) to one
    of the canonical CATEGORIES labels, or None if it doesn't match any.
    Keeps budgets.category and expenses.category using the same vocabulary —
    without this, a typo'd casing would silently never match any spend."""
    raw_normalized = raw.strip().lower()
    for label in CATEGORIES.values():
        if label.lower() == raw_normalized:
            return label
    return None
