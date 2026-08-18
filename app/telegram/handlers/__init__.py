"""Telegram bot handlers, split by concern:
- commands: /start, /help, /spend, /income, /salary, /audit, /setbudget, /log, /budgets, /ask, /reconcile
- messages: forwarded SMS text and receipt photos
- callbacks: the ask_user (cat:) and audit-anomaly (acat:) inline-button round trips

Re-exports every public handler so app.telegram.bot, app.routes, and the
test suite can keep importing `from app.telegram.handlers import handle_x`
regardless of which submodule actually owns it.
"""

from __future__ import annotations

from app.telegram.handlers.callbacks import (
    handle_audit_category_callback,
    handle_category_callback,
    send_ask_user_message,
    send_audit_question,
)
from app.telegram.handlers.commands import (
    handle_ask_command,
    handle_audit_command,
    handle_budgets_command,
    handle_help_command,
    handle_income_command,
    handle_log_command,
    handle_reconcile_command,
    handle_salary_command,
    handle_setbudget_command,
    handle_spend_command,
    handle_start_command,
)
from app.telegram.handlers.messages import handle_message, handle_photo_message

__all__ = [
    "handle_ask_command",
    "handle_audit_category_callback",
    "handle_audit_command",
    "handle_budgets_command",
    "handle_category_callback",
    "handle_help_command",
    "handle_income_command",
    "handle_log_command",
    "handle_message",
    "handle_photo_message",
    "handle_reconcile_command",
    "handle_salary_command",
    "handle_setbudget_command",
    "handle_spend_command",
    "handle_start_command",
    "send_ask_user_message",
    "send_audit_question",
]
