"""FastAPI app factory: builds the Telegram Application, wires its
startup/shutdown into the app lifespan, and includes app.routes.

Webhook vs polling is chosen via RUN_MODE — see the README for the tradeoff.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.logging_config import configure_logging
from app.routes import router
from app.telegram.bot import build_application, set_bot_commands

configure_logging()  # must run before anything below logs — see app/logging_config.py
logger = logging.getLogger(__name__)


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
        logger.info(
            "Telegram bot running in webhook mode — call setWebhook against POST <your-public-url>/telegram/webhook."
        )

    yield

    logger.info("Shutting down...")
    if settings.run_mode == "polling":
        assert application.updater is not None
        await application.updater.stop()
    await application.stop()
    await application.shutdown()


app = FastAPI(title="PocketAuditor", lifespan=lifespan)
app.include_router(router)
