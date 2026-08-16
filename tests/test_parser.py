"""Stage 3 tests: the regex-first parser must resolve well-formed SMS bodies
on its own (never touching the LLM), and must correctly route the ambiguous
ones to the LLM fallback.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.llm.base import LLMDecisionError
from app.parser import ParseError, parse_sms
from app.schemas import LLMExtraction


class _NeverCalledProvider:
    """Fails the test loudly if the regex pass wasn't actually confident
    enough to skip the LLM — this is what enforces "the cheap path stays
    cheap" rather than just hoping it does."""

    async def decide_match(self, transaction, candidates):  # pragma: no cover
        raise AssertionError("decide_match should not be called by the parser")

    async def parse_transaction(self, raw_text: str):  # pragma: no cover
        raise AssertionError(f"LLM fallback should not have been invoked for: {raw_text!r}")


class _FakeFallbackProvider:
    def __init__(self, extraction: LLMExtraction | None = None, error: Exception | None = None):
        self._extraction = extraction
        self._error = error
        self.calls = 0

    async def decide_match(self, transaction, candidates):  # pragma: no cover
        raise AssertionError("not used in these tests")

    async def parse_transaction(self, raw_text: str) -> LLMExtraction:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._extraction is not None
        return self._extraction


# --- clean, well-formed messages: regex alone must resolve these -----------

CLEAN_FIXTURES = [
    (
        "Rs.450.00 debited from A/c XX1234 on 10-08-26 to VPA blinkit@ybl BLINKIT. "
        "Not you? Call 18002586161 -HDFC Bank",
        Decimal("450.00"),
        "blinkit@ybl BLINKIT",
        date(2026, 8, 10),
        True,
    ),
    (
        "Dear Customer, Acct XX5678 debited with INR 1,250.00 on 05-Aug-26 towards "
        "AMAZON PAY. Avl Bal INR 34,210.55 -ICICI Bank",
        Decimal("1250.00"),
        "AMAZON PAY",
        date(2026, 8, 5),
        True,
    ),
    (
        "Your A/c XX9012 debited by Rs 799.00 on 12/08/26 and credited to SWIGGY. Avl Bal Rs 5,432.10 -SBI",
        Decimal("799.00"),
        "SWIGGY",
        date(2026, 8, 12),
        True,
    ),
    (
        "You have paid Rs.99 to Zomato via UPI Ref No 123456789012 on 14-08-26.",
        Decimal("99.00"),
        "Zomato",
        date(2026, 8, 14),
        True,
    ),
    (
        "Rs 2,499 spent on your HDFC Credit Card XX4321 at AMAZON on 09-08-26.",
        Decimal("2499.00"),
        "AMAZON",
        date(2026, 8, 9),
        True,
    ),
    (
        # Real-world P2P UPI transfer shape: "From <own account>" must NOT be
        # picked up as the merchant — only "To <recipient>" is the
        # counterparty. Regression case for a bug found in live testing.
        "Sent Rs.90.00\nFrom HDFC Bank A/C *9457\nTo MOHAMMED MUSABBAB AL EIL\n"
        "On 11/01/26\nRef 117001580517\nNot You?\nCall 18002586161/SMS BLOCK UPI to 7308080808",
        Decimal("90.00"),
        "MOHAMMED MUSABBAB AL EIL",
        date(2026, 1, 11),
        True,
    ),
]


@pytest.mark.parametrize("text,amount,merchant,txn_date,is_debit", CLEAN_FIXTURES)
async def test_clean_sms_resolved_by_regex_alone(text, amount, merchant, txn_date, is_debit):
    result = await parse_sms(text, provider=_NeverCalledProvider())
    assert result.method == "regex"
    assert result.amount == amount
    assert result.merchant == merchant
    assert result.txn_date == txn_date
    assert result.is_debit is is_debit
    assert result.confidence >= 0.75


async def test_credit_message_flagged_not_debit():
    text = "INR 25,000.00 credited to your A/c XX1234 on 01-08-26 by NEFT from EMPLOYER PVT LTD -HDFC Bank"
    result = await parse_sms(text, provider=_NeverCalledProvider())
    assert result.is_debit is False
    assert result.amount == Decimal("25000.00")


async def test_missing_date_still_resolved_by_regex_with_default_today():
    # amount (0.5) + merchant (0.25) = 0.75, exactly at (not below) the
    # threshold, so this must NOT trigger the LLM fallback.
    text = "Rs.150 debited from A/c XX1234 towards SWIGGY. Avl Bal Rs.900.00 -HDFC Bank"
    result = await parse_sms(text, provider=_NeverCalledProvider())
    assert result.method == "regex"
    assert result.amount == Decimal("150.00")
    assert result.merchant == "SWIGGY"
    assert result.txn_date == date.today()
    assert result.confidence == pytest.approx(0.75)


# --- ambiguous / low-confidence messages: must fall back to the LLM --------


async def test_low_confidence_message_routes_to_llm_fallback():
    # No date, no merchant, only amount -> confidence 0.5, below threshold.
    text = "Rs.75 debited. Avl Bal Rs.500.00"
    extraction = LLMExtraction(
        is_transaction=True, amount=75.0, merchant="Unknown", txn_date=date(2026, 8, 1), is_debit=True
    )
    provider = _FakeFallbackProvider(extraction=extraction)

    result = await parse_sms(text, provider=provider)

    assert provider.calls == 1
    assert result.method == "llm"
    assert result.amount == Decimal("75.00")
    assert result.merchant == "Unknown"
    assert result.txn_date == date(2026, 8, 1)


async def test_garbage_message_with_no_amount_raises_when_llm_also_fails():
    text = "Reminder: Please complete your KYC update by visiting the nearest branch. -Bank"
    provider = _FakeFallbackProvider(error=LLMDecisionError("model could not find a transaction"))

    with pytest.raises(ParseError):
        await parse_sms(text, provider=provider)
    assert provider.calls == 1


async def test_borderline_amount_present_llm_fails_falls_back_to_regex_result():
    # Amount found by regex (0.5) but LLM fallback errors out — since we do
    # have a usable amount, degrade to the regex result rather than losing
    # the transaction entirely.
    text = "Rs.75 debited. Avl Bal Rs.500.00"
    provider = _FakeFallbackProvider(error=LLMDecisionError("boom"))

    result = await parse_sms(text, provider=provider)

    assert result.method == "regex"
    assert result.amount == Decimal("75.00")
    assert result.confidence == pytest.approx(0.5)


async def test_garbage_message_never_calls_decide_match():
    # Sanity check that the parser only ever calls parse_transaction, never
    # decide_match, regardless of path.
    provider = _FakeFallbackProvider(
        extraction=LLMExtraction(is_transaction=True, amount=10.0, merchant=None, txn_date=date.today(), is_debit=True)
    )
    await parse_sms("some ambiguous text with Rs.10 in it somewhere", provider=provider)
    assert provider.calls == 1


# --- LLM says "not a transaction" — must not fabricate one anyway ----------


async def test_llm_says_not_a_transaction_raises_parse_error():
    """Regression: a stray non-transaction message (e.g. "Help") must not
    come back as a fabricated Rs.0.00 credit — the LLM saying
    is_transaction=False must be trusted and rejected, not smoothed over."""
    extraction = LLMExtraction(is_transaction=False, amount=None, merchant=None, txn_date=None, is_debit=None)
    provider = _FakeFallbackProvider(extraction=extraction)

    with pytest.raises(ParseError):
        await parse_sms("Help", provider=provider)
    assert provider.calls == 1


async def test_llm_says_transaction_but_omits_amount_still_raises():
    """Defensive guard: even if is_transaction=True, a missing amount means
    there's nothing to log — don't trust the flag alone."""
    extraction = LLMExtraction(is_transaction=True, amount=None, merchant=None, txn_date=None, is_debit=None)
    provider = _FakeFallbackProvider(extraction=extraction)

    with pytest.raises(ParseError):
        await parse_sms("some confusing text", provider=provider)
