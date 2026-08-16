"""Centralized settings. Nothing in the app should read os.environ directly —
everything goes through this Settings object, loaded once at import time."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Database -----------------------------------------------------
    database_url: str = Field(..., description="postgresql+asyncpg://... (Neon)")

    # --- Telegram -------------------------------------------------------
    telegram_bot_token: str = Field(..., description="BotFather token")
    telegram_webhook_secret: str | None = Field(
        default=None,
        description=(
            "Shared secret Telegram sends back on X-Telegram-Bot-Api-Secret-Token. "
            "The webhook rejects every request while this is unset — there is no "
            "'unauthenticated allowed' mode."
        ),
    )
    cron_secret: str | None = Field(
        default=None,
        description=(
            "Shared secret cron callers must send as X-Cron-Secret. /reconcile and "
            "/check-budgets reject every request while this is unset."
        ),
    )

    # --- LLM provider -----------------------------------------------------
    llm_provider: Literal["ollama", "claude", "gemini"] = "ollama"
    llm_model: str = "qwen2.5vl:7b"
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"

    # --- Agent behaviour --------------------------------------------------
    confidence_threshold: float = 0.75
    candidate_amount_tolerance_pct: float = 0.05
    candidate_date_window_days: int = 2

    # --- Runtime ------------------------------------------------------
    run_mode: Literal["webhook", "polling"] = "webhook"
    port: int = 7001


settings = Settings()  # type: ignore[call-arg]  # required env vars come from .env / real env
