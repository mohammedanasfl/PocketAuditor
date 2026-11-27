"""Centralized settings. Nothing in the app should read os.environ directly —
everything goes through this Settings object, loaded once at import time."""

from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, field_validator
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
    # Gemini 3.5 Flash-Lite for both local development and production (see
    # docs/AGENT_DESIGN.md §6). Claude is kept only to compare in the evals.
    llm_provider: Literal["gemini", "claude"] = "gemini"
    llm_model: str = "gemini-3.5-flash-lite"
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None

    # --- Agent behaviour --------------------------------------------------
    confidence_threshold: float = 0.75
    candidate_amount_tolerance_pct: float = 0.05
    candidate_date_window_days: int = 2
    # Independent from candidate_date_window_days even though both default to
    # 2 — one governs auto_link candidate matching, this one governs the
    # duplicate-charge guard; see CLAUDE.md's note on confidence_threshold vs.
    # _LLM_FALLBACK_THRESHOLD for the same "shared value, different knob" shape.
    duplicate_charge_window_days: int = 2

    # --- Tool-using agent (app/agent_core/) --------------------------------
    # "agent" = the tool-using agent decides each transaction (with the
    # policy layer checking every write); "pipeline" = the original fixed
    # decide_match flow. Both are kept so the evals can compare them.
    reconcile_engine: Literal["agent", "pipeline"] = "agent"
    agent_max_steps: int = 8  # model calls per run before it is stopped
    agent_max_tokens_per_run: int = 40_000
    agent_max_runs_per_day: int = 200  # per user — a cost safety net

    # --- Salary audit (Phase 4) -------------------------------------------
    # How close a credit must be to the profile's expected_salary to count as
    # "salary received" (fractional tolerance, e.g. 0.05 = ±5%).
    salary_match_tolerance_pct: float = 0.05
    # Days past the configured payday before a still-missing salary is flagged
    # "late" by the mid-month alert check.
    midmonth_alert_grace_days: int = 2

    # --- Access + locale --------------------------------------------------
    # Comma-separated Telegram chat ids allowed to use the bot, or "*" for
    # anyone. Empty (the default) allows nobody: the bot replies with the
    # sender's chat id so the owner can add it. Fail closed — a forgotten
    # setting must not let strangers spend your LLM budget.
    allowed_chat_ids: str = ""
    # All "today" / month boundaries use this timezone (see app/timeutil.py).
    timezone: str = "Asia/Kolkata"

    # --- Runtime ------------------------------------------------------
    run_mode: Literal["webhook", "polling"] = "webhook"
    port: int = 7001
    # The app's public https address, e.g. https://pocketauditor.onrender.com.
    # In webhook mode the bot registers <public_url>/telegram/webhook with
    # Telegram on startup. Render sets RENDER_EXTERNAL_URL automatically, so
    # on Render nothing needs to be configured.
    public_url: str | None = Field(default=None, validation_alias=AliasChoices("PUBLIC_URL", "RENDER_EXTERNAL_URL"))

    @field_validator("timezone")
    @classmethod
    def _timezone_must_exist(cls, value: str) -> str:
        """Fail at startup, not on the first message, if TIMEZONE is a typo."""
        try:
            ZoneInfo(value)
        except Exception as exc:  # ZoneInfo raises KeyError/ImportError, pydantic needs ValueError
            raise ValueError(f"unknown TIMEZONE {value!r}") from exc
        return value


settings = Settings()  # type: ignore[call-arg]  # required env vars come from .env / real env
