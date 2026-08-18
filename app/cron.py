"""The two cron-triggered, all-users background jobs — called from
app/routes.py's /reconcile and /check-budgets endpoints via BackgroundTasks.
Each opens one DB session per user rather than one for the whole batch, so a
single slow/erroring user can't hold a transaction open for the rest.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from telegram.ext import Application

from app.agent import reconcile_user
from app.audit import check_midmonth_alerts, run_monthly_audit
from app.budgets import check_budget_alerts
from app.db import SessionLocal
from app.models import User
from app.telegram.handlers import send_ask_user_message, send_audit_question

logger = logging.getLogger(__name__)


async def reconcile_all_users(application: Application) -> None:
    """The weekly-cron body: reconcile every known user."""
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


async def check_budgets_all_users(application: Application) -> None:
    """The daily-cron body: check budget alerts for every known user."""
    async with SessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
    logger.info("background check-budgets: %d user(s) to process", len(users))

    for user in users:
        async with SessionLocal() as session:
            alerts = await check_budget_alerts(session, user.id)
        for alert in alerts:
            logger.info("background check-budgets: user=%s — %s", user.id, alert.category)
            await application.bot.send_message(chat_id=user.telegram_chat_id, text=alert.as_message())


async def run_audit_all_users(application: Application) -> None:
    """The monthly-cron body: run the salary audit for every known user over
    the previous completed month. Skipped users (already audited, or no
    activity) get no message — only a completed audit is pushed to Telegram."""
    provider = application.bot_data["llm_provider"]

    async with SessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
    logger.info("background audit: %d user(s) to process", len(users))

    for user in users:
        async with SessionLocal() as session:
            result = await run_monthly_audit(session, provider, user.id)
        if result.status != "completed" or not result.message:
            logger.info("background audit: user=%s — %s, no message sent", user.id, result.status)
            continue
        await application.bot.send_message(chat_id=user.telegram_chat_id, text=result.message)
        for question in result.questions:
            await send_audit_question(application.bot, user.telegram_chat_id, question)


async def check_salary_alerts_all_users(application: Application) -> None:
    """The daily-cron body: fire proactive mid-month salary alerts (salary
    late / spending pace) for every known user. Same shape as
    check_budgets_all_users; check_midmonth_alerts itself dedups so a daily
    check doesn't mean daily nagging."""
    async with SessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
    logger.info("background check-salary-alerts: %d user(s) to process", len(users))

    for user in users:
        async with SessionLocal() as session:
            alerts = await check_midmonth_alerts(session, user.id)
        for alert in alerts:
            logger.info("background check-salary-alerts: user=%s — %s", user.id, alert.alert_type)
            await application.bot.send_message(chat_id=user.telegram_chat_id, text=alert.as_message())
