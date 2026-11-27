"""Recognize money moving between your own accounts, and label income.

The user registers their savings account's last 4 digits (/myaccount
savings 4321). Then:

- a DEBIT that sends money TO that account  → a savings transfer (filed
  under the Savings category — not spending);
- a CREDIT that brings money FROM that account → a savings withdrawal
  (not income).

How we tell "to" from "from": we find where the account number appears in
the SMS and look right around it.
    "... debited ... and credited to a/c XXXXXXXX4321"  → to   → transfer
    "... credited to A/c XX1234 from A/c XX4321"         → from → withdrawal
    "A/c XX4321 debited for Rs.500 ... to SWIGGY"        → from → a normal spend

A credit SMS that puts money INTO the savings account (from the savings
bank itself) is ignored — the transfer is counted from the debit side.
"""

from __future__ import annotations

import re
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OwnAccount, SalaryProfile

SAVINGS_WITHDRAWAL = "savings_withdrawal"

# A preposition followed by at most 3 short words, right before the account
# number: "to A/c ", "credited to a/c no. ", "From HDFC Bank A/C ", "Benef A/c ".
# Further away, the preposition belongs to something else ("sent to Ramesh
# via UPI. A/c XX4321 debited" — the "to" is about Ramesh).
_TO_BEFORE_RE = re.compile(r"\b(?:to|benef|beneficiary)\b\W+(?:[\w/.]+\W+){0,3}$", re.IGNORECASE)
_FROM_BEFORE_RE = re.compile(r"\b(?:from|by)\b\W+(?:[\w/.]+\W+){0,3}$", re.IGNORECASE)
# The account itself is what got debited/credited: "A/c XX4321 debited ...".
_DEBITED_AFTER_RE = re.compile(r"^\W*(?:is\s+|has been\s+|was\s+)?(?:debited|transferred)\b", re.IGNORECASE)
_CREDITED_AFTER_RE = re.compile(r"^\W*(?:is\s+|has been\s+|was\s+)?credited\b", re.IGNORECASE)
_REFUND_RE = re.compile(r"\b(?:refund|refunded|reversal|reversed|cashback)\b", re.IGNORECASE)
_SALARY_RE = re.compile(r"\b(?:salary|sal)\b", re.IGNORECASE)


def _account_mention_re(last4: str) -> re.Pattern[str]:
    """An account number ending in last4, written the way banks write it:
    XX4321, *4321, X4321, XXXXXXXX4321, 'ending 4321', 'A/c no. 4321' — and
    ICICI's 3-digit mask XX321 (only after X/* so a random '321' never counts)."""
    last3 = re.escape(last4[1:])
    return re.compile(
        rf"(?:[xX*]+\d*{re.escape(last4)}|[xX*]+{last3}|ending\s*{re.escape(last4)}"
        rf"|a/c\s*(?:no\.?\s*)?{re.escape(last4)}|ac\s+{re.escape(last4)})\b",
        re.IGNORECASE,
    )


def account_direction(text: str, last4: str) -> str | None:
    """Where the money goes relative to this account:
    'to'   — money goes INTO it   ("... credited to A/c XX4321", "A/c XX4321 credited"),
    'from' — money comes OUT of it ("... from A/c XX4321", "A/c XX4321 debited"),
    None   — not mentioned, or unclear."""
    for mention in _account_mention_re(last4).finditer(text):
        text_before = text[max(0, mention.start() - 40) : mention.start()]
        text_after = text[mention.end() : mention.end() + 25]
        if _CREDITED_AFTER_RE.search(text_after) or _TO_BEFORE_RE.search(text_before):
            return "to"
        if _DEBITED_AFTER_RE.search(text_after) or _FROM_BEFORE_RE.search(text_before):
            return "from"
    return None


async def savings_accounts(session: AsyncSession, user_id: UUID) -> list[str]:
    """Last 4 digits of every savings account the user registered."""
    rows = await session.execute(
        select(OwnAccount.last4).where(OwnAccount.user_id == user_id, OwnAccount.kind == "savings")
    )
    return list(rows.scalars().all())


def is_transfer_to_savings(text: str, savings_last4s: list[str]) -> bool:
    """A debit SMS that sends money INTO the savings account."""
    return any(account_direction(text, last4) == "to" for last4 in savings_last4s)


def is_money_arriving_in_savings(text: str, savings_last4s: list[str]) -> bool:
    """A credit SMS from the savings account's own bank ("A/c XX4321 credited
    with Rs.35,000 from XX1234"). It's the other half of a transfer that is
    counted from the main account's debit SMS — counting it again as income
    would double it."""
    return any(account_direction(text, last4) == "to" for last4 in savings_last4s)


_TO_AN_ACCOUNT_RE = re.compile(r"\bto\s+(?:your\s+)?(?:a/c|ac|acct|account)\b", re.IGNORECASE)


def is_savings_side_of_a_transfer(text: str, is_debit: bool, savings_last4s: list[str]) -> bool:
    """The savings bank's own SMS for a move between your accounts:
    - a credit INTO savings ("A/c XX4321 credited with Rs.35,000 from XX1234"), or
    - a debit OUT of savings to another account ("Rs.3000 transferred from
      A/c XX4321 to A/c XX1234").
    The same move is counted from your main account's SMS, so this side is
    skipped — otherwise it would count twice (as income, or as spending)."""
    if not is_debit:
        return is_money_arriving_in_savings(text, savings_last4s)
    leaves_savings = any(account_direction(text, last4) == "from" for last4 in savings_last4s)
    return leaves_savings and _TO_AN_ACCOUNT_RE.search(text) is not None


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
