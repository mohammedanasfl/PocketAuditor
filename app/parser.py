"""Regex-first SMS parser with an LLM fallback for the ambiguous residue.

Built for Indian bank/UPI SMS formats: INR / Rs. / ₹ currency markers,
DD-MM-YY(YY) and DD-Mon-YYYY dates, and "at/to/towards/VPA/from <merchant>"
merchant phrasing. The regex pass is deliberately cheap and imperfect — its
job is to resolve the clear-cut majority of messages without an LLM call;
anything it isn't confident about gets one LLM call via the same
LLMProvider interface the agent loop uses (app/llm/base.py), never a
provider-specific import.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from app.llm.base import LLMDecisionError, LLMProvider

logger = logging.getLogger(__name__)

# Confidence below this triggers an LLM fallback call. Distinct from
# app.config.settings.confidence_threshold, which gates the *agent's*
# match decisions (Stage 4) — the two happen to share the same 0.75 value
# but are conceptually independent knobs.
_LLM_FALLBACK_THRESHOLD = 0.75

_AMOUNT_RE = re.compile(
    r"(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d{1,2})?)|\b([\d,]+(?:\.\d{1,2})?)\s*(?:INR|Rs\.?|₹)\b",
    re.IGNORECASE,
)
_DEBIT_RE = re.compile(
    r"\b(?:debited|debit|spent|paid|withdrawn|purchase|sent|transferred)\b", re.IGNORECASE
)
_CREDIT_RE = re.compile(r"\b(?:credited|credit|received|refunded|deposited)\b", re.IGNORECASE)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})\b")
_DATE_MON_RE = re.compile(
    r"\b(\d{1,2})[-\s](jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-\s](\d{2,4})\b",
    re.IGNORECASE,
)

_MERCHANT_STOPWORDS = r"on|dt|ref|txn|for|A/c|Avl|via|Not|Call|by|and"
# Two separate patterns rather than one shared "at|to|towards|from|VPA"
# alternation: in a debit message "from" precedes the *user's own* account
# ("debited from A/c XX1234"), not the counterparty, while in a credit
# message it's the other way ("credited ... from EMPLOYER"). Blending both
# directions into one pattern picks whichever preposition appears first in
# the text rather than the one that's semantically the counterparty — e.g.
# "Sent Rs.90 From HDFC Bank A/C *9457 To MOHAMMED ..." would otherwise match
# "HDFC Bank" (the source account) instead of the actual recipient.
_MERCHANT_TO_RE = re.compile(
    rf"(?:(?:at|to|towards)\s+(?:VPA\s+)?|VPA\s+)"
    rf"([A-Za-z0-9][A-Za-z0-9&.\-_@ ]{{1,40}}?)"
    rf"(?=\s+(?:{_MERCHANT_STOPWORDS})\b|[.,]|\s+-|$)",
    re.IGNORECASE,
)
_MERCHANT_FROM_RE = re.compile(
    rf"from\s+([A-Za-z0-9][A-Za-z0-9&.\-_@ ]{{1,40}}?)"
    rf"(?=\s+(?:{_MERCHANT_STOPWORDS})\b|[.,]|\s+-|$)",
    re.IGNORECASE,
)
# Prepositional phrases that regex-match syntactically ("to your A/c...") but
# aren't a real merchant name.
_MERCHANT_FALSE_POSITIVES = {"your", "you", "the", "a/c", "account", "acct", "our"}


class ParseError(RuntimeError):
    """Raised when neither the regex pass nor the LLM fallback can find a
    transaction in the text at all (e.g. the message isn't actually a
    transaction alert)."""


@dataclass(frozen=True)
class ParsedTransaction:
    amount: Decimal
    merchant: str | None
    txn_date: date
    is_debit: bool
    confidence: float
    method: Literal["regex", "llm"]


def _parse_amount(text: str) -> Decimal | None:
    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    raw = (match.group(1) or match.group(2)).replace(",", "")
    try:
        return Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _parse_direction(text: str) -> bool | None:
    if _DEBIT_RE.search(text):
        return True
    if _CREDIT_RE.search(text):
        return False
    return None


def _parse_date(text: str) -> date | None:
    match = _DATE_NUMERIC_RE.search(text)
    if match:
        day_s, month_s, year_s = match.groups()
        year = int(year_s) if len(year_s) == 4 else 2000 + int(year_s)
        try:
            return date(year, int(month_s), int(day_s))
        except ValueError:
            return None

    match = _DATE_MON_RE.search(text)
    if match:
        day_s, month_name, year_s = match.groups()
        year = int(year_s) if len(year_s) == 4 else 2000 + int(year_s)
        month = _MONTHS[month_name.lower()]
        try:
            return date(year, month, int(day_s))
        except ValueError:
            return None

    return None


def _first_merchant_match(pattern: re.Pattern[str], text: str) -> str | None:
    for match in pattern.finditer(text):
        candidate = match.group(1).strip()
        if candidate.lower() not in _MERCHANT_FALSE_POSITIVES:
            return candidate
    return None


def _parse_merchant(text: str, is_debit: bool | None) -> str | None:
    """Try the preposition that names the counterparty for this direction
    first, falling back to the other if that phrasing isn't present."""
    if is_debit is False:
        return _first_merchant_match(_MERCHANT_FROM_RE, text) or _first_merchant_match(
            _MERCHANT_TO_RE, text
        )
    return _first_merchant_match(_MERCHANT_TO_RE, text) or _first_merchant_match(
        _MERCHANT_FROM_RE, text
    )


def _regex_result(
    *, amount: Decimal, merchant: str | None, txn_date: date | None,
    is_debit: bool | None, confidence: float,
) -> ParsedTransaction:
    return ParsedTransaction(
        amount=amount,
        merchant=merchant,
        txn_date=txn_date or date.today(),
        is_debit=is_debit if is_debit is not None else True,
        confidence=confidence,
        method="regex",
    )


async def parse_sms(raw_text: str, provider: LLMProvider) -> ParsedTransaction:
    """Parse a forwarded SMS body into a ParsedTransaction.

    Raises ParseError if neither the regex pass nor the LLM fallback can
    locate a transaction amount at all — i.e. the text likely isn't a
    transaction alert.
    """
    amount = _parse_amount(raw_text)
    is_debit = _parse_direction(raw_text)
    txn_date = _parse_date(raw_text)
    merchant = _parse_merchant(raw_text, is_debit)

    confidence = 0.0
    if amount is not None:
        confidence += 0.5
    if txn_date is not None:
        confidence += 0.25
    if merchant is not None:
        confidence += 0.25

    # confidence can only reach the threshold if amount contributed (0.5),
    # since date+merchant alone cap out at 0.5 — so amount is guaranteed set
    # here.
    if confidence >= _LLM_FALLBACK_THRESHOLD:
        logger.info(
            "parsed via regex (confidence=%.2f): amount=%s merchant=%r txn_date=%s is_debit=%s",
            confidence, amount, merchant, txn_date, is_debit,
        )
        return _regex_result(
            amount=amount, merchant=merchant, txn_date=txn_date,
            is_debit=is_debit, confidence=confidence,
        )

    logger.info(
        "regex confidence %.2f below %.2f threshold — falling back to LLM",
        confidence, _LLM_FALLBACK_THRESHOLD,
    )
    try:
        extraction = await provider.parse_transaction(raw_text)
    except LLMDecisionError as exc:
        if amount is not None:
            # Regex found a usable amount even though overall confidence was
            # low (e.g. missing date/merchant); better to log an
            # approximate expense than drop it because the LLM fallback
            # itself failed.
            logger.warning(
                "LLM fallback failed (%s) — using regex result anyway (confidence=%.2f)", exc, confidence
            )
            return _regex_result(
                amount=amount, merchant=merchant, txn_date=txn_date,
                is_debit=is_debit, confidence=confidence,
            )
        logger.warning("could not extract a transaction from this message: %s", exc)
        raise ParseError(f"Could not extract a transaction from this message: {exc}") from exc

    logger.info(
        "parsed via llm: amount=%s merchant=%r txn_date=%s is_debit=%s",
        extraction.amount, extraction.merchant, extraction.txn_date, extraction.is_debit,
    )
    return ParsedTransaction(
        amount=Decimal(str(extraction.amount)).quantize(Decimal("0.01")),
        merchant=extraction.merchant,
        txn_date=extraction.txn_date,
        is_debit=extraction.is_debit,
        confidence=1.0,
        method="llm",
    )
