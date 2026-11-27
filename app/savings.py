"""Recognize money moving between your own accounts, and label income.

The user registers their savings account's last 4 digits (/myaccount
savings 4321). Then:

- a DEBIT that sends money TO that account  → a savings transfer (filed
  under the Savings category — not spending);
- a CREDIT that brings money FROM that account → a savings withdrawal
  (not income).

How we tell "to" from "from": we find where the account number appears in
the SMS and look at the nearest preposition before it.
    "... debited ... and credited to a/c XXXXXXXX4321"  → to   → transfer
    "... credited to A/c XX1234 from A/c XX4321"         → from → withdrawal
"""

from __future__ import annotations

import re
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OwnAccount, SalaryProfile

SAVINGS_WITHDRAWAL = "savings_withdrawal"

_PREPOSITION_RE = re.compile(r"\b(to|from|by)\b", re.IGNORECASE)
_REFUND_RE = re.compile(r"\b(?:refund|refunded|reversal|reversed|cashback)\b", re.IGNORECASE)
_SALARY_RE = re.compile(r"\b(?:salary|sal)\b", re.IGNORECASE)


def _account_mention_re(last4: str) -> re.Pattern[str]:
    """An account number ending in last4, written the way banks write it:
    XX4321, *4321, X4321, XXXXXXXX4321, 'ending 4321', 'A/c no. 4321'."""
    return re.compile(rf"(?:[xX*]+\d*|ending\s*|a/c\s*(?:no\.?\s*)?|ac\s+){re.escape(last4)}\b", re.IGNORECASE)


def account_direction(text: str, last4: str) -> str | None:
    """'to' if the SMS sends money to this account, 'from' if it brings money
    from it, None if the account isn't mentioned (or it's unclear)."""
    for mention in _account_mention_re(last4).finditer(text):
        text_before = text[max(0, mention.start() - 40) : mention.start()]
        prepositions = _PREPOSITION_RE.findall(text_before)
        if not prepositions:
            continue
        nearest = prepositions[-1].lower()
        return "to" if nearest == "to" else "from"
    return None


async def savings_accounts(session: AsyncSession, user_id: UUID) -> list[str]:
    """Last 4 digits of every savings account the user registered."""
    rows = await session.execute(
        select(OwnAccount.last4).where(OwnAccount.user_id == user_id, OwnAccount.kind == "savings")
    )
    return list(rows.scalars().all())


def is_transfer_to_savings(text: str, savings_last4s: list[str]) -> bool:
    return any(account_direction(text, last4) == "to" for last4 in savings_last4s)


def is_withdrawal_from_savings(text: str, savings_last4s: list[str]) -> bool:
    return any(account_direction(text, last4) == "from" for last4 in savings_last4s)


def income_kind(
    text: str, amount: Decimal, savings_last4s: list[str], profile: SalaryProfile | None, tolerance_pct: float
) -> str:
    """Label a credit. Checked in this order — the first match wins."""
    if is_withdrawal_from_savings(text, savings_last4s):
        return SAVINGS_WITHDRAWAL
    if _REFUND_RE.search(text):
        return "refund"
    if _SALARY_RE.search(text):
        return "salary"
    if profile is not None and profile.expected_salary > 0:
        tolerance = profile.expected_salary * Decimal(str(tolerance_pct))
        if abs(amount - profile.expected_salary) <= tolerance:
            return "salary"
    return "other"
