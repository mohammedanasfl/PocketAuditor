"""The provider-agnostic contract.

app/agent.py and app/parser.py depend only on this file and app/schemas.py —
never on `anthropic` or `httpx` directly — so the provider swap and unit
tests stay cheap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.schemas import AuditReport, ExtractedReceipt, LLMExtraction, MatchDecision, QueryIntent


class LLMDecisionError(RuntimeError):
    """Raised when a provider cannot produce a schema-valid response, even
    after its retry budget (if any) is exhausted."""


# --- Tool calling (used by the agent loop in app/agent_core/) ---------------
# Our own small, provider-neutral types. Each provider converts to and from
# its SDK's format, so the agent loop never sees a vendor type.


@dataclass
class ToolSpec:
    """A tool the model may call: name, what it does, and a JSON Schema for
    its arguments (generated from a Pydantic model, never hand-written)."""

    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    """One tool call the model asked for."""

    id: str
    name: str
    args: dict


@dataclass
class AssistantTurn:
    """The model's reply: either text, or one or more tool calls (or both).

    `raw` is the provider's own copy of the reply. It is sent back unchanged
    in the next request, because some models (Gemini 3.x) attach hidden
    "thought signatures" that must be returned as-is."""

    text: str
    tool_calls: list[ToolCall]
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Any = None


@dataclass
class ChatMessage:
    """One entry of the conversation the agent loop keeps.

    - role "user": `text` is the user's message or the goal.
    - role "assistant": `turn` is what the model replied.
    - role "tool": `tool_call` is the call being answered, `result` its output.
    """

    role: Literal["user", "assistant", "tool"]
    text: str = ""
    turn: AssistantTurn | None = None
    tool_call: ToolCall | None = None
    result: dict = field(default_factory=dict)


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

    async def chat_with_tools(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> AssistantTurn:
        """One step of the agent loop: send the conversation so far plus the
        available tools, get back text and/or tool calls. Raises
        LLMDecisionError on any provider failure."""
        ...

    async def audit_finances(self, snapshot: dict) -> AuditReport:
        """Write the monthly salary-audit findings (Phase 4) from a
        pre-computed financial snapshot. Returns prose + which anomaly
        candidates to flag — never numbers; app/audit.py computes every figure
        and re-checks the flagged ids in code."""
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
