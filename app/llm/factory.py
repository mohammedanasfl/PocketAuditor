"""Constructs the configured LLMProvider once at startup.

Fails loudly on an unknown LLM_PROVIDER rather than deferring the error to
the first request that needs it.
"""

from __future__ import annotations

from app.config import settings
from app.llm.base import LLMProvider


def get_provider() -> LLMProvider:
    if settings.llm_provider == "ollama":
        from app.llm.ollama import OllamaProvider

        return OllamaProvider()
    if settings.llm_provider == "claude":
        from app.llm.claude import ClaudeProvider

        return ClaudeProvider()
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
