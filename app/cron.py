"""The cron-triggered, all-users background jobs — called from app/routes.py's
/reconcile, /check-budgets, /run-audit, /check-salary-alerts, and
/send-weekly-digest endpoints via BackgroundTasks. Each opens one DB session
per user rather than one for the whole batch, so a single slow/erroring user
can't hold a transaction open for the rest.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram.ext import Application

from app.agent_core.reconcile import reconcile
from app.audit import check_midmonth_alerts, run_monthly_audit
from app.budgets import check_budget_alerts
from app.config import settings
from app.db import SessionLocal
from app.digest import build_weekly_digest
from app.models import User
from app.telegram.access import is_chat_allowed
from app.telegram.handlers import send_ask_user_message, send_audit_question

logger = logging.getLogger(__name__)


async def _allowed_users(session: AsyncSession) -> list[User]:
    """Every user still on ALLOWED_CHAT_IDS — someone removed from the list
    gets no more cron messages or LLM runs."""
    users = (await session.execute(select(User))).scalars().all()
    return [user for user in users if is_chat_allowed(user.telegram_chat_id, settings.allowed_chat_ids)]


async def reconcile_all_users(application: Application) -> None:
    """The weekly-cron body: reconcile every known user."""
    provider = application.bot_data["llm_provider"]

    async with SessionLocal() as session:
        users = await _allowed_users(session)
    logger.info("background reconcile: %d user(s) to process", len(users))

    for user in users:
        try:
            async with SessionLocal() as session:
                summary = await reconcile(session, provider, user.id)
            logger.info("background reconcile: user=%s — %s", user.id, summary.as_message())

            for pending in summary.pending_questions:
                await send_ask_user_message(application.bot, user.telegram_chat_id, pending)

            await application.bot.send_message(chat_id=user.telegram_chat_id, text=summary.as_message())
        except Exception:
            # One user's failure (e.g. they blocked the bot) must not stop
            # the run for everyone after them.
            logger.exception("background reconcile: user=%s failed, continuing with the next user", user.id)


async def check_budgets_all_users(application: Application) -> None:
    """The daily-cron body: check budget alerts for every known user."""
    async with SessionLocal() as session:
        users = await _allowed_users(session)
    logger.info("background check-budgets: %d user(s) to process", len(users))

    for user in users:
        try:
            async with SessionLocal() as session:
                alerts = await check_budget_alerts(session, user.id)
            for alert in alerts:
                logger.info("background check-budgets: user=%s — %s", user.id, alert.category)
                await application.bot.send_message(chat_id=user.telegram_chat_id, text=alert.as_message())
        except Exception:
            logger.exception("background check-budgets: user=%s failed, continuing with the next user", user.id)


async def run_audit_all_users(application: Application) -> None:
    """The monthly-cron body: run the salary audit for every known user over
    the previous completed month. Skipped users (already audited, or no
    activity) get no message — only a completed audit is pushed to Telegram."""
    provider = application.bot_data["llm_provider"]

    async with SessionLocal() as session:
        users = await _allowed_users(session)
    logger.info("background audit: %d user(s) to process", len(users))

    for user in users:
        try:
            async with SessionLocal() as session:
                result = await run_monthly_audit(session, provider, user.id)
            if result.status != "completed" or not result.message:
                logger.info("background audit: user=%s — %s, no message sent", user.id, result.status)
                continue
            await application.bot.send_message(chat_id=user.telegram_chat_id, text=result.message)
            for question in result.questions:
                await send_audit_question(application.bot, user.telegram_chat_id, question)
        except Exception:
            logger.exception("background audit: user=%s failed, continuing with the next user", user.id)


async def check_salary_alerts_all_users(application: Application) -> None:
    """The daily-cron body: fire proactive mid-month salary alerts (salary
    late / spending pace) for every known user. Same shape as
    check_budgets_all_users; check_midmonth_alerts itself dedups so a daily
    check doesn't mean daily nagging."""
    async with SessionLocal() as session:
        users = await _allowed_users(session)
    logger.info("background check-salary-alerts: %d user(s) to process", len(users))

    for user in users:
        try:
            async with SessionLocal() as session:
                alerts = await check_midmonth_alerts(session, user.id)
            for alert in alerts:
                logger.info("background check-salary-alerts: user=%s — %s", user.id, alert.alert_type)
                await application.bot.send_message(chat_id=user.telegram_chat_id, text=alert.as_message())
        except Exception:
            logger.exception("background check-salary-alerts: user=%s failed, continuing with the next user", user.id)


async def send_weekly_digest_all_users(application: Application) -> None:
    """The weekly-cron body: send every known user their deterministic (no
    LLM) weekly spend digest. Same shape as check_budgets_all_users — no
    llm_provider needed."""
    async with SessionLocal() as session:
        users = await _allowed_users(session)
    logger.info("background send-weekly-digest: %d user(s) to process", len(users))

    for user in users:
        try:
            async with SessionLocal() as session:
                digest = await build_weekly_digest(session, user.id)
            logger.info("background send-weekly-digest: user=%s", user.id)
            await application.bot.send_message(chat_id=user.telegram_chat_id, text=digest.as_message())
        except Exception:
            logger.exception("background send-weekly-digest: user=%s failed, continuing with the next user", user.id)
