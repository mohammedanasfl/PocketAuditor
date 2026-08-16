"""Ollama-backed LLMProvider — local Qwen for dev.

LLM_MODEL (qwen2.5vl:7b by default) is expected to be vision-capable, since
the same configured model is used for decide_match/parse_transaction *and*
extract_receipt. If it's swapped back to a text-only model (e.g. qwen2.5:7b),
extract_receipt's behavior depends entirely on how that model/Ollama handles
an unsupported `images` field — that's the same "configure the right model"
trust boundary decide_match/parse_transaction already rely on, not something
this provider guards against separately.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import date

import httpx
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

_MAX_ATTEMPTS = 2


class OllamaProvider:
    """Implements LLMProvider against a local Ollama server's /api/chat endpoint.

    Ollama's grammar-constrained decoding is applied via the `format` field
    (a JSON Schema), same as Claude's structured outputs — but a 7B model can
    still emit malformed JSON under constrained decoding on rare inputs, so
    this provider gets one retry with the validation error fed back into the
    prompt before giving up.
    """

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self._base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self._model = model or settings.llm_model

    async def _chat_json(self, system: str, user: str, schema: dict, images: list[str] | None = None) -> str:
        user_message: dict = {"role": "user", "content": user}
        if images is not None:
            user_message["images"] = images
        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                response = await client.post(
                    f"{self._base_url}/api/chat",
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": system},
                            user_message,
                        ],
                        "format": sanitize_schema_for_llm(schema),
                        "stream": False,
                        "options": {"temperature": 0},
                    },
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                # Never let httpx leak past this module — e.g. Ollama returns
                # 400 if `images` is sent to a non-vision model, or the server
                # is unreachable. Callers only ever handle LLMDecisionError.
                raise LLMDecisionError(f"Ollama request failed: {exc}") from exc
            return response.json()["message"]["content"]

    async def decide_match(self, transaction: dict, candidates: list[dict]) -> MatchDecision:
        logger.info("Ollama.decide_match: model=%s candidates=%d", self._model, len(candidates))
        payload = build_decide_payload(transaction, candidates)
        schema = MatchDecision.model_json_schema()
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            content = await self._chat_json(DECIDE_SYSTEM, payload, schema)
            try:
                decision = MatchDecision.model_validate_json(content)
                logger.info("Ollama.decide_match: action=%s confidence=%.2f", decision.action, decision.confidence)
                return decision
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning(
                    "Ollama.decide_match: invalid response on attempt %d/%d: %s", attempt, _MAX_ATTEMPTS, exc
                )
                payload = (
                    f"{payload}\n\n(Your previous response was invalid: {exc}. "
                    "Return exactly one JSON object matching the schema.)"
                )
        raise LLMDecisionError(f"Ollama returned an invalid MatchDecision after retrying: {last_error}")

    async def parse_transaction(self, raw_text: str) -> LLMExtraction:
        logger.info("Ollama.parse_transaction: model=%s", self._model)
        payload = build_parse_payload(raw_text, reference_date=date.today())
        schema = LLMExtraction.model_json_schema()
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            content = await self._chat_json(PARSE_SYSTEM, payload, schema)
            try:
                extraction = LLMExtraction.model_validate_json(content)
                logger.info("Ollama.parse_transaction: amount=%s merchant=%r", extraction.amount, extraction.merchant)
                return extraction
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning(
                    "Ollama.parse_transaction: invalid response on attempt %d/%d: %s", attempt, _MAX_ATTEMPTS, exc
                )
                payload = (
                    f"{payload}\n\n(Your previous response was invalid: {exc}. "
                    "Return exactly one JSON object matching the schema.)"
                )
        raise LLMDecisionError(f"Ollama returned an invalid LLMExtraction after retrying: {last_error}")

    async def extract_receipt(self, image_bytes: bytes, mime_type: str) -> ExtractedReceipt:
        logger.info("Ollama.extract_receipt: model=%s mime_type=%s bytes=%d", self._model, mime_type, len(image_bytes))
        image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        payload = "Extract the transaction details from this image."
        schema = ExtractedReceipt.model_json_schema()
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            content = await self._chat_json(EXTRACT_RECEIPT_SYSTEM, payload, schema, images=[image_b64])
            try:
                receipt = ExtractedReceipt.model_validate_json(content)
                logger.info(
                    "Ollama.extract_receipt: readable=%s confidence=%.2f total=%s",
                    receipt.readable,
                    receipt.confidence,
                    receipt.total_amount,
                )
                return receipt
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning(
                    "Ollama.extract_receipt: invalid response on attempt %d/%d: %s", attempt, _MAX_ATTEMPTS, exc
                )
                payload = (
                    f"{payload}\n\n(Your previous response was invalid: {exc}. "
                    "Return exactly one JSON object matching the schema.)"
                )
        raise LLMDecisionError(f"Ollama returned an invalid ExtractedReceipt after retrying: {last_error}")

    async def interpret_query(self, question: str) -> QueryIntent:
        logger.info("Ollama.interpret_query: model=%s", self._model)
        payload = build_interpret_query_payload(question, reference_date=date.today())
        schema = QueryIntent.model_json_schema()
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            content = await self._chat_json(INTERPRET_QUERY_SYSTEM, payload, schema)
            try:
                intent = QueryIntent.model_validate_json(content)
                logger.info(
                    "Ollama.interpret_query: aggregation=%s category=%r date_range=%s",
                    intent.aggregation,
                    intent.category,
                    intent.date_range,
                )
                return intent
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning(
                    "Ollama.interpret_query: invalid response on attempt %d/%d: %s", attempt, _MAX_ATTEMPTS, exc
                )
                payload = (
                    f"{payload}\n\n(Your previous response was invalid: {exc}. "
                    "Return exactly one JSON object matching the schema.)"
                )
        raise LLMDecisionError(f"Ollama returned an invalid QueryIntent after retrying: {last_error}")
