"""Remove sensitive numbers from text before it is sent to an LLM.

Bank SMS often contain phone numbers, UPI reference numbers and card or
account numbers. The model never needs them to understand a transaction, so
they are masked. Amounts are left alone: they are short (or written with
commas) and the model needs them.
"""

from __future__ import annotations

import re

# 9 or more digits in a row: phone numbers (10), UPI refs (12), card/account
# numbers. Amounts are at most 8 digits without commas (Rs.1,00,00,000 is
# already written with commas in bank SMS).
_LONG_NUMBER_RE = re.compile(r"\d{9,}")


def redact(text: str) -> str:
    """Replace every run of 9+ digits with [number]."""
    return _LONG_NUMBER_RE.sub("[number]", text)
