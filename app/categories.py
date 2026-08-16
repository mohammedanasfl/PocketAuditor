"""The one canonical expense-category vocabulary — shared by /setbudget,
the ask_user inline buttons, photo captions, and the agent's own auto_log
category (app/agent.py). Lives outside app/telegram/ so the core decision
loop doesn't have to depend on the Telegram-presentation layer for it.
"""

from __future__ import annotations

# Short keys keep callback_data ("cat:<run_id>:<key>") well under Telegram's
# 64-byte cap even with a full 36-char UUID — see app/telegram/keyboards.py.
CATEGORIES: dict[str, str] = {
    "food": "Food",
    "transport": "Transport",
    "shopping": "Shopping",
    "bills": "Bills",
    "health": "Health",
    "entertainment": "Entertainment",
    "other": "Other",
}


def normalize_category(raw: str) -> str | None:
    """Case-insensitively match free-text input (e.g. from /setbudget, a
    photo caption, or the model's own suggested_category) to one of the
    canonical CATEGORIES labels, or None if it doesn't match any. Keeps
    budgets.category and expenses.category using the same vocabulary —
    without this, a typo'd casing (or an LLM inventing "Groceries" instead
    of "Food") would silently never match any spend."""
    raw_normalized = raw.strip().lower()
    for label in CATEGORIES.values():
        if label.lower() == raw_normalized:
            return label
    return None
