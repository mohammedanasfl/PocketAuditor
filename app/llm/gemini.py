"""Gemini-backed LLMProvider — the default provider for development and production.

Uses response_json_schema (not the older response_schema) because Pydantic's
generated schemas express nullable fields (`X | None`) via `anyOf`, which the
older OpenAPI-subset `response_schema` doesn't reliably support — the newer
`response_json_schema` accepts full JSON Schema.
"""

from __future__ import annotations

import json
import logging

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError

from app.config import settings
from app.llm.base import (
    AssistantTurn,
    ChatMessage,
    LLMDecisionError,
    ToolCall,
    ToolSpec,
    sanitize_schema_for_llm,
)
from app.llm.prompts import (
    AUDIT_SYSTEM,
    DECIDE_SYSTEM,
    EXTRACT_RECEIPT_SYSTEM,
    INTERPRET_QUERY_SYSTEM,
    PARSE_SYSTEM,
    build_audit_payload,
    build_decide_payload,
    build_interpret_query_payload,
    build_parse_payload,
)
from app.schemas import AuditReport, ExtractedReceipt, LLMExtraction, MatchDecision, QueryIntent
from app.timeutil import local_today

logger = logging.getLogger(__name__)


class GeminiProvider:
    """Implements LLMProvider against Google's Gemini API using structured
    outputs (response_json_schema + response_mime_type=application/json) —
    the same schema-constrained-JSON contract Claude is held to.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._client = genai.Client(api_key=api_key or settings.gemini_api_key)
        self._model = model or settings.llm_model

    async def _generate_json(self, system: str, contents: list, schema: dict) -> str:
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_json_schema=sanitize_schema_for_llm(schema),
                    # No tools are ever passed here, so disable automatic function
                    # calling outright rather than let the SDK warn on every call.
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
        except (genai_errors.APIError, httpx.HTTPError) as exc:
            # Rate limits, 5xx, bad key — callers only ever handle
            # LLMDecisionError, so never let an SDK exception escape.
            raise LLMDecisionError(f"Gemini request failed: {exc}") from exc
        if response.text is None:
            raise LLMDecisionError("Gemini returned no text content (likely blocked or truncated)")
        return response.text

    async def decide_match(self, transaction: dict, candidates: list[dict]) -> MatchDecision:
        logger.info("Gemini.decide_match: model=%s candidates=%d", self._model, len(candidates))
        payload = build_decide_payload(transaction, candidates)
        content = await self._generate_json(DECIDE_SYSTEM, [payload], MatchDecision.model_json_schema())
        try:
            decision = MatchDecision.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Gemini.decide_match: invalid response: %s", exc)
            raise LLMDecisionError(f"Gemini returned an invalid MatchDecision: {exc}") from exc
        logger.info("Gemini.decide_match: action=%s confidence=%.2f", decision.action, decision.confidence)
        return decision

    async def parse_transaction(self, raw_text: str) -> LLMExtraction:
        logger.info("Gemini.parse_transaction: model=%s", self._model)
        payload = build_parse_payload(raw_text, reference_date=local_today())
        content = await self._generate_json(PARSE_SYSTEM, [payload], LLMExtraction.model_json_schema())
        try:
            extraction = LLMExtraction.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Gemini.parse_transaction: invalid response: %s", exc)
            raise LLMDecisionError(f"Gemini returned an invalid LLMExtraction: {exc}") from exc
        logger.info("Gemini.parse_transaction: amount=%s merchant=%r", extraction.amount, extraction.merchant)
        return extraction

    async def extract_receipt(self, image_bytes: bytes, mime_type: str) -> ExtractedReceipt:
        logger.info("Gemini.extract_receipt: model=%s mime_type=%s bytes=%d", self._model, mime_type, len(image_bytes))
        content = await self._generate_json(
            EXTRACT_RECEIPT_SYSTEM,
            [
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                "Extract the transaction details from this image.",
            ],
            ExtractedReceipt.model_json_schema(),
        )
        try:
            receipt = ExtractedReceipt.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Gemini.extract_receipt: invalid response: %s", exc)
            raise LLMDecisionError(f"Gemini returned an invalid ExtractedReceipt: {exc}") from exc
        logger.info(
            "Gemini.extract_receipt: readable=%s confidence=%.2f total=%s",
            receipt.readable,
            receipt.confidence,
            receipt.total_amount,
        )
        return receipt

    async def audit_finances(self, snapshot: dict) -> AuditReport:
        logger.info("Gemini.audit_finances: model=%s", self._model)
        payload = build_audit_payload(snapshot)
        content = await self._generate_json(AUDIT_SYSTEM, [payload], AuditReport.model_json_schema())
        try:
            report = AuditReport.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Gemini.audit_finances: invalid response: %s", exc)
            raise LLMDecisionError(f"Gemini returned an invalid AuditReport: {exc}") from exc
        logger.info(
            "Gemini.audit_finances: recommendations=%d flagged=%d confidence=%.2f",
            len(report.recommendations),
            len(report.flagged_expense_ids),
            report.confidence,
        )
        return report

    async def interpret_query(self, question: str) -> QueryIntent:
        logger.info("Gemini.interpret_query: model=%s", self._model)
        payload = build_interpret_query_payload(question, reference_date=local_today())
        content = await self._generate_json(INTERPRET_QUERY_SYSTEM, [payload], QueryIntent.model_json_schema())
        try:
            intent = QueryIntent.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Gemini.interpret_query: invalid response: %s", exc)
            raise LLMDecisionError(f"Gemini returned an invalid QueryIntent: {exc}") from exc
        logger.info(
            "Gemini.interpret_query: aggregation=%s category=%r date_range=%s",
            intent.aggregation,
            intent.category,
            intent.date_range,
        )
        return intent

    # --- tool calling (agent loop) ----------------------------------------

    async def chat_with_tools(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec]) -> AssistantTurn:
        logger.info("Gemini.chat_with_tools: model=%s messages=%d tools=%d", self._model, len(messages), len(tools))
        declarations = [
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters_json_schema=sanitize_schema_for_llm(tool.parameters),
            )
            for tool in tools
        ]
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=_to_gemini_contents(messages),
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    tools=[types.Tool(function_declarations=declarations)],
                    # We run the tools ourselves (after the policy check), so
                    # the SDK must never call Python functions on its own.
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
        except (genai_errors.APIError, httpx.HTTPError) as exc:
            raise LLMDecisionError(f"Gemini request failed: {exc}") from exc

        if not response.candidates or response.candidates[0].content is None:
            raise LLMDecisionError("Gemini returned no content (likely blocked)")
        content = response.candidates[0].content

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for part in content.parts or []:
            call = part.function_call
            if call is not None and call.name:
                call_id = call.id or f"call_{len(tool_calls)}"
                tool_calls.append(ToolCall(id=call_id, name=call.name, args=dict(call.args or {})))
            elif part.text and not part.thought:
                text_parts.append(part.text)

        usage = response.usage_metadata
        return AssistantTurn(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            output_tokens=(usage.candidates_token_count or 0) if usage else 0,
            raw=content,
        )


def _to_gemini_contents(messages: list[ChatMessage]) -> list[types.Content]:
    """Convert our ChatMessages to Gemini Contents. Tool results that follow
    each other go into one Content, as Gemini expects for parallel calls."""
    contents: list[types.Content] = []
    for message in messages:
        if message.role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=message.text)]))
        elif message.role == "assistant":
            assert message.turn is not None
            # Send the model's own reply back unchanged (keeps thought signatures).
            contents.append(message.turn.raw)
        else:
            assert message.tool_call is not None
            part = types.Part.from_function_response(name=message.tool_call.name, response=message.result)
            gemini_gave_an_id = not message.tool_call.id.startswith("call_")  # "call_N" ids are our own
            if part.function_response is not None and gemini_gave_an_id:
                part.function_response.id = message.tool_call.id
            previous = contents[-1] if contents else None
            if previous is not None and previous.role == "user" and _is_tool_results(previous):
                assert previous.parts is not None
                previous.parts.append(part)
            else:
                contents.append(types.Content(role="user", parts=[part]))
    return contents


def _is_tool_results(content: types.Content) -> bool:
    return bool(content.parts) and all(part.function_response is not None for part in content.parts or [])
