"""Dev tool: feed one fixture transaction + candidates through a real
provider and print the decision. This is the check that actually proves the
LLMProvider abstraction holds against a live model — the unit tests only
prove it against mocks (see tests/test_llm_providers.py).

Usage:
    python -m scripts.try_decide            # uses LLM_PROVIDER from .env
    python -m scripts.try_decide --both      # runs Ollama AND Claude side by side
                                              # (requires ANTHROPIC_API_KEY)
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from app.config import settings
from app.llm.factory import get_provider

TRANSACTION = {"amount": "450.00", "merchant": "Blinkit", "txn_date": "2026-08-10"}
CANDIDATE_ID = str(uuid.uuid4())
CANDIDATES = [
    {
        "id": CANDIDATE_ID,
        "amount": "450.00",
        "category": "Groceries",
        "merchant": "Blinkit",
        "txn_date": "2026-08-10",
        "notes": None,
    }
]

RAW_SMS = "Rs.450.00 debited from A/c XX1234 on 10-08-26 to VPA blinkit@ybl BLINKIT. -HDFC Bank"

QUESTION = "how much did I spend on food this week"


async def _run(label: str, provider) -> None:
    print(f"\n=== {label} ===")
    decision = await provider.decide_match(TRANSACTION, CANDIDATES)
    print("decide_match ->", decision)
    extraction = await provider.parse_transaction(RAW_SMS)
    print("parse_transaction ->", extraction)
    intent = await provider.interpret_query(QUESTION)
    print("interpret_query ->", intent)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--both", action="store_true", help="run both Ollama and Claude side by side")
    args = parser.parse_args()

    if args.both:
        from app.llm.claude import ClaudeProvider
        from app.llm.ollama import OllamaProvider

        await _run("Ollama", OllamaProvider())
        await _run("Claude", ClaudeProvider())
    else:
        await _run(settings.llm_provider, get_provider())


if __name__ == "__main__":
    asyncio.run(main())
