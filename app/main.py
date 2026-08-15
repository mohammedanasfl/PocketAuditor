"""FastAPI app: Telegram webhook receiver, the /reconcile cron endpoint, and
/health for the keep-alive ping.

Webhook vs polling is chosen via RUN_MODE — see the README for the tradeoff.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from sqlalchemy import select
from telegram import Update
from telegram.ext import Application

from app.agent import reconcile_user
from app.budgets import check_budget_alerts
from app.config import settings
from app.db import SessionLocal
from app.logging_config import configure_logging
from app.models import User
from app.telegram.bot import build_application
from app.telegram.handlers import send_ask_user_message

configure_logging()  # must run before anything below logs — see app/logging_config.py
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up (RUN_MODE=%s, LLM_PROVIDER=%s)...", settings.run_mode, settings.llm_provider)
    application = build_application()
    await application.initialize()
    await application.start()
    app.state.application = application

    if settings.run_mode == "polling":
        await application.updater.start_polling()
        logger.info("Telegram bot running in polling mode — send it a message.")
    else:
        logger.info(
            "Telegram bot running in webhook mode — call setWebhook against "
            "POST <your-public-url>/telegram/webhook."
        )

    yield

    logger.info("Shutting down...")
    if settings.run_mode == "polling":
        await application.updater.stop()
    await application.stop()
    await application.shutdown()


app = FastAPI(title="PocketAuditor", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    if (
        settings.telegram_webhook_secret
        and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="invalid secret token")

    application: Application = request.app.state.application
    data = await request.json()
    logger.info("webhook: received update_id=%s", data.get("update_id"))
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}


async def _run_reconcile_all_users(application: Application) -> None:
    """The weekly-cron body: reconcile every known user, one DB session per
    user so a slow/erroring user can't hold a transaction open for the rest."""
    provider = application.bot_data["llm_provider"]

    async with SessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
    logger.info("background reconcile: %d user(s) to process", len(users))

    for user in users:
        async with SessionLocal() as session:
            summary = await reconcile_user(session, provider, user.id)
        logger.info("background reconcile: user=%s — %s", user.id, summary.as_message())

        for pending in summary.pending_questions:
            await send_ask_user_message(application.bot, user.telegram_chat_id, pending)

        await application.bot.send_message(chat_id=user.telegram_chat_id, text=summary.as_message())


@app.post("/reconcile", status_code=202)
async def reconcile_endpoint(background_tasks: BackgroundTasks) -> dict:
    """Triggers the weekly agent run for every user. Runs in the background
    and returns immediately — GitHub Actions gets a fast response regardless
    of how large the backlog is; the per-user summaries land in Telegram
    when each user's loop finishes."""
    logger.info("POST /reconcile — scheduling background run for all users")
    background_tasks.add_task(_run_reconcile_all_users, app.state.application)
    return {"status": "scheduled"}


async def _run_check_budgets_all_users(application: Application) -> None:
    """The daily-cron body: check budget alerts for every known user, one DB
    session per user — same isolation reasoning as _run_reconcile_all_users."""
    async with SessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
    logger.info("background check-budgets: %d user(s) to process", len(users))

    for user in users:
        async with SessionLocal() as session:
            alerts = await check_budget_alerts(session, user.id)
        for alert in alerts:
            logger.info("background check-budgets: user=%s — %s", user.id, alert.category)
            await application.bot.send_message(chat_id=user.telegram_chat_id, text=alert.as_message())


@app.post("/check-budgets", status_code=202)
async def check_budgets_endpoint(background_tasks: BackgroundTasks) -> dict:
    """Triggers the daily budget-alert check for every user. Same
    fire-and-return-202 shape as /reconcile."""
    logger.info("POST /check-budgets — scheduling background run for all users")
    background_tasks.add_task(_run_check_budgets_all_users, app.state.application)
    return {"status": "scheduled"}
