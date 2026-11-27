"""Remove sensitive numbers from text before it is sent to an LLM.

Bank SMS often contain phone numbers, UPI reference numbers and card or
account numbers. The model never needs them to understand a transaction, so
they are masked. Amounts are left alone: they are short (or written with
commas) and the model needs them.
"""

from __future__ import annotations

import re

# Each pattern is one kind of sensitive number. Amounts are never matched:
# they are at most 8 digits without commas (bank SMS write Rs.1,00,00,000
# with commas).
_SENSITIVE_NUMBER_PATTERNS = [
    re.compile(r"\b\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4}\b"),  # card: 4111 1111 1111 1111
    re.compile(r"\+91[\s-]?\d{5}[\s-]?\d{5}\b"),  # phone: +91 98765 43210
    re.compile(r"\b1800[\s-]\d{3}[\s-]\d{4}\b"),  # toll-free: 1800-258-6161
    re.compile(r"\d{9,}"),  # any 9+ digits: phones, UPI refs, account numbers
]


def redact(text: str) -> str:
    """Replace sensitive numbers with [number]."""
    for pattern in _SENSITIVE_NUMBER_PATTERNS:
        text = pattern.sub("[number]", text)
    return text
