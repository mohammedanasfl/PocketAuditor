"""LLM providers used by the eval runner.

- NoLLMProvider: every LLM call fails. Measures what the plain code (regex
  parser, guards, merchant memory) gets right on its own — and runs offline.
- CountingProvider: wraps a real provider and counts calls + time spent, so
  the report can show how many LLM calls each suite needed.
"""

from __future__ import annotations

import time

from app.llm.base import LLMDecisionError, LLMProvider
from app.schemas import AuditReport, ExtractedReceipt, LLMExtraction, MatchDecision, QueryIntent


class NoLLMProvider:
    """Every call fails, exactly like a real provider that is down."""

    async def decide_match(self, transaction: dict, candidates: list[dict]) -> MatchDecision:
        raise LLMDecisionError("no LLM in this eval run")

    async def parse_transaction(self, raw_text: str) -> LLMExtraction:
        raise LLMDecisionError("no LLM in this eval run")

    async def extract_receipt(self, image_bytes: bytes, mime_type: str) -> ExtractedReceipt:
        raise LLMDecisionError("no LLM in this eval run")

    async def interpret_query(self, question: str) -> QueryIntent:
        raise LLMDecisionError("no LLM in this eval run")

    async def audit_finances(self, snapshot: dict) -> AuditReport:
        raise LLMDecisionError("no LLM in this eval run")


class CountingProvider:
    """Passes every call through to `inner`, counting calls and seconds."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.calls = 0
        self.seconds = 0.0

    def reset(self) -> None:
        self.calls = 0
        self.seconds = 0.0

    def _start(self) -> float:
        self.calls += 1
        return time.perf_counter()

    def _stop(self, started: float) -> None:
        self.seconds += time.perf_counter() - started

    async def decide_match(self, transaction: dict, candidates: list[dict]) -> MatchDecision:
        started = self._start()
        try:
            return await self.inner.decide_match(transaction, candidates)
        finally:
            self._stop(started)

    async def parse_transaction(self, raw_text: str) -> LLMExtraction:
        started = self._start()
        try:
            return await self.inner.parse_transaction(raw_text)
        finally:
            self._stop(started)

    async def extract_receipt(self, image_bytes: bytes, mime_type: str) -> ExtractedReceipt:
        started = self._start()
        try:
            return await self.inner.extract_receipt(image_bytes, mime_type)
        finally:
            self._stop(started)

    async def interpret_query(self, question: str) -> QueryIntent:
        started = self._start()
        try:
            return await self.inner.interpret_query(question)
        finally:
            self._stop(started)

    async def audit_finances(self, snapshot: dict) -> AuditReport:
        started = self._start()
        try:
            return await self.inner.audit_finances(snapshot)
        finally:
            self._stop(started)


def make_provider(name: str, model: str | None) -> LLMProvider:
    """'none', 'gemini' or 'claude'. Real providers read their API key from
    the normal settings (.env)."""
    if name == "none":
        return NoLLMProvider()
    if name == "gemini":
        from app.llm.gemini import GeminiProvider

        return GeminiProvider(model=model)
    if name == "claude":
        from app.llm.claude import ClaudeProvider

        return ClaudeProvider(model=model)
    raise ValueError(f"unknown provider {name!r} (use none, gemini or claude)")
