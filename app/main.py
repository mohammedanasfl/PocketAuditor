"""FastAPI app factory: builds the Telegram Application, wires its
startup/shutdown into the app lifespan, and includes app.routes.

Webhook vs polling is chosen via RUN_MODE — see the README for the tradeoff.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from telegram.ext import Application

from app.config import settings
from app.logging_config import configure_logging
from app.routes import router, wait_for_background_updates
from app.telegram.bot import build_application, set_bot_commands

configure_logging()  # must run before anything below logs — see app/logging_config.py
logger = logging.getLogger(__name__)


async def register_webhook(application: Application) -> None:
    """Webhook mode: tell Telegram where to send messages, so a fresh deploy
    works without any manual setWebhook call. Needs PUBLIC_URL (set
    automatically on Render) and TELEGRAM_WEBHOOK_SECRET."""
    if not settings.public_url or not settings.telegram_webhook_secret:
        logger.warning(
            "Webhook mode, but PUBLIC_URL or TELEGRAM_WEBHOOK_SECRET is not set — not registering the webhook. "
            "Set both, or call setWebhook yourself against <your-public-url>/telegram/webhook."
        )
        return
    url = settings.public_url.rstrip("/") + "/telegram/webhook"
    await application.bot.set_webhook(url=url, secret_token=settings.telegram_webhook_secret)
    logger.info("Telegram bot running in webhook mode — webhook registered at %s", url)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting up (RUN_MODE=%s, LLM_PROVIDER=%s)...", settings.run_mode, settings.llm_provider)
    application = build_application()
    await application.initialize()
    await application.start()
    await set_bot_commands(application)
    app.state.application = application

    if settings.run_mode == "polling":
        assert application.updater is not None
        await application.updater.start_polling()
        logger.info("Telegram bot running in polling mode — send it a message.")
    else:
        await register_webhook(application)

    yield

    logger.info("Shutting down...")
    # Finish updates we already answered Telegram for — Telegram won't resend them.
    await wait_for_background_updates()
    if settings.run_mode == "polling":
        assert application.updater is not None
        await application.updater.stop()
    await application.stop()
    await application.shutdown()


app = FastAPI(title="PocketAuditor", lifespan=lifespan)
app.include_router(router)
