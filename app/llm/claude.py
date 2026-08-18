"""Claude-backed LLMProvider — claude-sonnet-5 in prod (per LLM_MODEL)."""

from __future__ import annotations

import base64
import json
import logging
from datetime import date

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from app.config import settings
from app.llm.base import LLMDecisionError, sanitize_schema_for_llm
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

logger = logging.getLogger(__name__)


class ClaudeProvider:
    """Implements LLMProvider against the Claude Messages API using structured
    outputs (output_config.format) so the response is constrained to the same
    schema Ollama is constrained to.

    Deliberately does not set temperature/top_p: claude-sonnet-5 (and the rest
    of the 4.6+ model family) reject non-default sampling parameters with a
    400. Determinism instead comes from the constrained output schema plus
    low effort.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._client = AsyncAnthropic(api_key=api_key or settings.anthropic_api_key)
        self._model = model or settings.llm_model

    async def _create_json(self, system: str, user: str | list[dict], schema: dict) -> str:
        # Plain dicts here don't structurally match the SDK's strict
        # MessageParam/OutputConfigParam TypedDicts closely enough for mypy's
        # overload resolution — tests/test_llm_providers.py exercises the
        # actual request shape against a mocked endpoint instead.
        response = await self._client.messages.create(  # type: ignore[call-overload]
            model=self._model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": "low",
                "format": {
                    "type": "json_schema",
                    "schema": sanitize_schema_for_llm(schema),
                },
            },
        )
        text_block = next(block for block in response.content if block.type == "text")
        return text_block.text

    async def decide_match(self, transaction: dict, candidates: list[dict]) -> MatchDecision:
        logger.info("Claude.decide_match: model=%s candidates=%d", self._model, len(candidates))
        payload = build_decide_payload(transaction, candidates)
        content = await self._create_json(DECIDE_SYSTEM, payload, MatchDecision.model_json_schema())
        try:
            decision = MatchDecision.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Claude.decide_match: invalid response: %s", exc)
            raise LLMDecisionError(f"Claude returned an invalid MatchDecision: {exc}") from exc
        logger.info("Claude.decide_match: action=%s confidence=%.2f", decision.action, decision.confidence)
        return decision

    async def parse_transaction(self, raw_text: str) -> LLMExtraction:
        logger.info("Claude.parse_transaction: model=%s", self._model)
        payload = build_parse_payload(raw_text, reference_date=date.today())
        content = await self._create_json(PARSE_SYSTEM, payload, LLMExtraction.model_json_schema())
        try:
            extraction = LLMExtraction.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Claude.parse_transaction: invalid response: %s", exc)
            raise LLMDecisionError(f"Claude returned an invalid LLMExtraction: {exc}") from exc
        logger.info("Claude.parse_transaction: amount=%s merchant=%r", extraction.amount, extraction.merchant)
        return extraction

    async def extract_receipt(self, image_bytes: bytes, mime_type: str) -> ExtractedReceipt:
        logger.info("Claude.extract_receipt: model=%s mime_type=%s bytes=%d", self._model, mime_type, len(image_bytes))
        image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        content = await self._create_json(
            EXTRACT_RECEIPT_SYSTEM,
            [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": image_b64},
                },
                {"type": "text", "text": "Extract the transaction details from this image."},
            ],
            ExtractedReceipt.model_json_schema(),
        )
        try:
            receipt = ExtractedReceipt.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Claude.extract_receipt: invalid response: %s", exc)
            raise LLMDecisionError(f"Claude returned an invalid ExtractedReceipt: {exc}") from exc
        logger.info(
            "Claude.extract_receipt: readable=%s confidence=%.2f total=%s",
            receipt.readable,
            receipt.confidence,
            receipt.total_amount,
        )
        return receipt

    async def audit_finances(self, snapshot: dict) -> AuditReport:
        logger.info("Claude.audit_finances: model=%s", self._model)
        payload = build_audit_payload(snapshot)
        content = await self._create_json(AUDIT_SYSTEM, payload, AuditReport.model_json_schema())
        try:
            report = AuditReport.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Claude.audit_finances: invalid response: %s", exc)
            raise LLMDecisionError(f"Claude returned an invalid AuditReport: {exc}") from exc
        logger.info(
            "Claude.audit_finances: recommendations=%d flagged=%d confidence=%.2f",
            len(report.recommendations),
            len(report.flagged_expense_ids),
            report.confidence,
        )
        return report

    async def interpret_query(self, question: str) -> QueryIntent:
        logger.info("Claude.interpret_query: model=%s", self._model)
        payload = build_interpret_query_payload(question, reference_date=date.today())
        content = await self._create_json(INTERPRET_QUERY_SYSTEM, payload, QueryIntent.model_json_schema())
        try:
            intent = QueryIntent.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Claude.interpret_query: invalid response: %s", exc)
            raise LLMDecisionError(f"Claude returned an invalid QueryIntent: {exc}") from exc
        logger.info(
            "Claude.interpret_query: aggregation=%s category=%r date_range=%s",
            intent.aggregation,
            intent.category,
            intent.date_range,
        )
        return intent
