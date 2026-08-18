"""FastAPI route declarations: the Telegram webhook receiver, the /reconcile,
/check-budgets, /run-audit, /check-salary-alerts, and /send-weekly-digest cron
endpoints, and /health for the keep-alive ping.

No business logic lives here — the two cron endpoints just schedule a
BackgroundTasks call into app.cron and return immediately (202) so the
caller — GitHub Actions for the cron endpoints — gets a fast response
regardless of how large the backlog is; the per-user summaries land in
Telegram when each user's loop finishes.

Every non-/health route is shared-secret gated, and every check fails
*closed*: an unset secret rejects all callers rather than admitting them, so
forgetting to configure one in production breaks the route loudly (Telegram
gets no bot replies at all / the cron gets 401s) instead of silently
accepting forged Telegram updates or letting anyone who finds the URL
trigger an LLM run for every user in the database.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from telegram import Update
from telegram.ext import Application

from app.config import settings
from app.cron import (
    check_budgets_all_users,
    check_salary_alerts_all_users,
    reconcile_all_users,
    run_audit_all_users,
    send_weekly_digest_all_users,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_cron_secret(x_cron_secret: str | None = Header(default=None)) -> None:
    if not settings.cron_secret or not secrets.compare_digest(x_cron_secret or "", settings.cron_secret):
        raise HTTPException(status_code=401, detail="invalid cron secret")


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    if not settings.telegram_webhook_secret or not secrets.compare_digest(
        x_telegram_bot_api_secret_token or "", settings.telegram_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="invalid secret token")

    application: Application = request.app.state.application
    data = await request.json()
    logger.info("webhook: received update_id=%s", data.get("update_id"))
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}


@router.post("/reconcile", status_code=202, dependencies=[Depends(_verify_cron_secret)])
async def reconcile_endpoint(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Triggers the weekly agent run for every user."""
    logger.info("POST /reconcile — scheduling background run for all users")
    background_tasks.add_task(reconcile_all_users, request.app.state.application)
    return {"status": "scheduled"}


@router.post("/check-budgets", status_code=202, dependencies=[Depends(_verify_cron_secret)])
async def check_budgets_endpoint(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Triggers the daily budget-alert check for every user. Same
    fire-and-return-202 shape as /reconcile."""
    logger.info("POST /check-budgets — scheduling background run for all users")
    background_tasks.add_task(check_budgets_all_users, request.app.state.application)
    return {"status": "scheduled"}


@router.post("/run-audit", status_code=202, dependencies=[Depends(_verify_cron_secret)])
async def run_audit_endpoint(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Triggers the monthly salary audit for every user (previous completed
    month). Same fire-and-return-202, cron-secret-gated shape as /reconcile."""
    logger.info("POST /run-audit — scheduling background run for all users")
    background_tasks.add_task(run_audit_all_users, request.app.state.application)
    return {"status": "scheduled"}


@router.post("/check-salary-alerts", status_code=202, dependencies=[Depends(_verify_cron_secret)])
async def check_salary_alerts_endpoint(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Triggers the daily mid-month salary-alert check for every user. Same
    fire-and-return-202, cron-secret-gated shape as /check-budgets."""
    logger.info("POST /check-salary-alerts — scheduling background run for all users")
    background_tasks.add_task(check_salary_alerts_all_users, request.app.state.application)
    return {"status": "scheduled"}


@router.post("/send-weekly-digest", status_code=202, dependencies=[Depends(_verify_cron_secret)])
async def send_weekly_digest_endpoint(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Triggers the weekly spend digest for every user. Same
    fire-and-return-202, cron-secret-gated shape as /reconcile."""
    logger.info("POST /send-weekly-digest — scheduling background run for all users")
    background_tasks.add_task(send_weekly_digest_all_users, request.app.state.application)
    return {"status": "scheduled"}
