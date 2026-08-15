"""The provider-agnostic contract.

app/agent.py and app/parser.py depend only on this file and app/schemas.py —
never on `anthropic` or `httpx` directly — so the provider swap and unit
tests stay cheap.
"""

from __future__ import annotations

from typing import Protocol

from app.schemas import ExtractedReceipt, LLMExtraction, MatchDecision, QueryIntent


class LLMDecisionError(RuntimeError):
    """Raised when a provider cannot produce a schema-valid response, even
    after its retry budget (if any) is exhausted."""


class LLMProvider(Protocol):
    async def decide_match(self, transaction: dict, candidates: list[dict]) -> MatchDecision:
        """Decide what to do with one transaction given up to 3 expense candidates."""
        ...

    async def parse_transaction(self, raw_text: str) -> LLMExtraction:
        """Fallback extraction when the regex parser's confidence is too low."""
        ...

    async def extract_receipt(self, image_bytes: bytes, mime_type: str) -> ExtractedReceipt:
        """Extract transaction fields from a photo of a bill/receipt/payment
        confirmation. `readable=False` must be trustworthy on its own — the
        Telegram photo handler (Phase 2) uses it, not just confidence, to
        decide whether to create a transaction at all."""
        ...

    async def interpret_query(self, question: str) -> QueryIntent:
        """Turn a natural-language question (Phase 3b's /ask) into a fixed
        query intent — never SQL, never anything app/query.py would need to
        trust beyond its declared shape."""
        ...


# JSON Schema keywords Pydantic emits (e.g. from Field(ge=..., le=...)) that
# structured-output schemas commonly don't support. Stripped before the schema
# is sent to either provider; range/length are still enforced afterward via
# the Pydantic model itself (model_validate_json), so nothing goes unchecked —
# it just moves from "the provider promises to honor it" to "we verify it."
_UNSUPPORTED_JSON_SCHEMA_KEYS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
}


def sanitize_schema_for_llm(schema: dict) -> dict:
    """Recursively strip unsupported JSON Schema keywords before handing the
    schema to a provider's structured-output / grammar-constrained mode."""
    if isinstance(schema, dict):
        return {
            key: sanitize_schema_for_llm(value)
            for key, value in schema.items()
            if key not in _UNSUPPORTED_JSON_SCHEMA_KEYS
        }
    if isinstance(schema, list):
        return [sanitize_schema_for_llm(item) for item in schema]
    return schema
