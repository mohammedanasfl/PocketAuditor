"""Gemini-backed LLMProvider — free-tier alternative to Claude for prod.

Uses response_json_schema (not the older response_schema) because Pydantic's
generated schemas express nullable fields (`X | None`) via `anyOf`, which the
older OpenAPI-subset `response_schema` doesn't reliably support — the newer
`response_json_schema` accepts full JSON Schema.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.config import settings
from app.llm.base import LLMDecisionError, sanitize_schema_for_llm
from app.llm.prompts import (
    DECIDE_SYSTEM,
    EXTRACT_RECEIPT_SYSTEM,
    INTERPRET_QUERY_SYSTEM,
    PARSE_SYSTEM,
    build_decide_payload,
    build_interpret_query_payload,
    build_parse_payload,
)
from app.schemas import ExtractedReceipt, LLMExtraction, MatchDecision, QueryIntent

logger = logging.getLogger(__name__)


class GeminiProvider:
    """Implements LLMProvider against Google's Gemini API using structured
    outputs (response_json_schema + response_mime_type=application/json) —
    the same schema-constrained-JSON contract Claude/Ollama are held to.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._client = genai.Client(api_key=api_key or settings.gemini_api_key)
        self._model = model or settings.llm_model

    async def _generate_json(self, system: str, contents: list, schema: dict) -> str:
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
        payload = build_parse_payload(raw_text, reference_date=date.today())
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
            receipt.readable, receipt.confidence, receipt.total_amount,
        )
        return receipt

    async def interpret_query(self, question: str) -> QueryIntent:
        logger.info("Gemini.interpret_query: model=%s", self._model)
        payload = build_interpret_query_payload(question, reference_date=date.today())
        content = await self._generate_json(INTERPRET_QUERY_SYSTEM, [payload], QueryIntent.model_json_schema())
        try:
            intent = QueryIntent.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Gemini.interpret_query: invalid response: %s", exc)
            raise LLMDecisionError(f"Gemini returned an invalid QueryIntent: {exc}") from exc
        logger.info(
            "Gemini.interpret_query: aggregation=%s category=%r date_range=%s",
            intent.aggregation, intent.category, intent.date_range,
        )
        return intent
