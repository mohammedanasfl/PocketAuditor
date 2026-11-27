"""The `/command` handlers. No decision logic lives here — reconciliation
routes through app.agent, expense creation through app.expenses, and
budgets/queries through their own service modules."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.ext import ContextTypes

from app.agent_core.reconcile import reconcile
from app.audit import run_monthly_audit
from app.budgets import format_budgets_message, get_budget_statuses, upsert_budget
from app.categories import CATEGORIES, normalize_category
from app.db import SessionLocal, insert_once
from app.expenses import log_manual_expense
from app.income import get_income_summary
from app.models import OwnAccount
from app.month_plan import get_month_plan
from app.reports import get_spend_summary
from app.salary import format_salary_profile, get_salary_profile, upsert_salary_profile
from app.savings import own_accounts
from app.telegram.handlers._shared import _get_or_create_user
from app.telegram.handlers.callbacks import send_ask_user_message, send_audit_question
from app.telegram.handlers.messages import answer_chat_message

logger = logging.getLogger(__name__)


_WELCOME_TEXT = (
    "👋 Welcome to PocketAuditor!\n\n"
    "I reconcile your bank/UPI SMS alerts against your expense ledger, so you "
    "don't have to check every transaction by hand.\n\n"
    "How it works:\n"
    "1️⃣ Forward me your bank/UPI SMS alerts as they arrive, or send a photo "
    "of a bill/receipt. I read the amount, merchant and date and handle it "
    "right away:\n"
    "   • match it to an expense you already logged\n"
    "   • log it myself, if it's clear-cut\n"
    "   • ask you to pick a category — only when I'm genuinely unsure\n"
    "2️⃣ Salary and savings: forward your salary SMS and the SMS when you move "
    "money to savings (any amount — it can change every month). Tell me your "
    "savings account once with /myaccount. I'll show what's left to spend and "
    "warn you before you run out.\n"
    '3️⃣ Ask me anything in plain words: "how much did I spend on food this week?", '
    '"how much is left?". I\'ll ask you to confirm before changing anything.\n\n'
    "Commands:\n"
    "/start — show this message\n"
    "/reconcile — reconcile your pending transactions\n"
    "/spend — see your spend totals for this week, month, year, and all time\n"
    "/income — see your income totals (from forwarded credit/salary SMS)\n"
    "/salary <expected> [savings_target] [payday] — set your expected monthly "
    "salary so I can audit it, e.g. /salary 50000 10000 1\n"
    "/audit — audit last month: income vs spend, savings, and AI recommendations\n"
    "/log <category> <amount> [notes] — manually log cash or other spend with "
    "no bank alert at all, e.g. /log Food 900 lunch\n"
    "/setbudget <category> <amount> — set a monthly limit, e.g. /setbudget Food 4000\n"
    "/budgets — see your limits and this month's spend per category\n"
    "/plan — this month so far: salary, saved, spending money, money left\n"
    "/myaccount savings <last 4 digits> — your own savings account, so transfers to it count as saving\n"
    '/ask <question> — or just type it: "how much on food this week?", '
    '"set my shopping budget to 3000" (I\'ll ask you to confirm any change)\n\n'
    "Forward your first SMS whenever you're ready."
)


async def handle_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    logger.info("chat=%s: /start", update.effective_chat.id)
    await update.message.reply_text(_WELCOME_TEXT)


async def handle_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    logger.info("chat=%s: /help", update.effective_chat.id)
    await update.message.reply_text(_WELCOME_TEXT)


async def handle_spend_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    logger.info("chat=%s: /spend", chat_id)

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        summary = await get_spend_summary(session, user.id)

    await update.message.reply_text(summary.as_message())


async def handle_income_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/income — income totals for this month, last month, this year, all time.
    Reads the `incomes` ledger populated from forwarded credit SMS."""
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    logger.info("chat=%s: /income", chat_id)

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        summary = await get_income_summary(session, user.id)

    await update.message.reply_text(summary.as_message())


async def handle_salary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/salary <expected> [savings_target] [payday] — set expected monthly
    salary, an optional monthly savings target, and an optional payday
    (day-of-month). With no arguments, shows the current profile.

    Grounds the monthly audit and the mid-month "salary late" / "spending
    pace" alerts — without an expected figure there's nothing to compare
    against."""
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    args = context.args or []
    logger.info("chat=%s: /salary %r", chat_id, args)

    usage = (
        "Usage: /salary <expected> [savings_target] [payday]\n"
        "e.g. /salary 50000 10000 1  (expect Rs.50,000, save Rs.10,000/month, paid on the 1st)"
    )

    if not args:
        async with SessionLocal() as session:
            user = await _get_or_create_user(session, chat_id)
            profile = await get_salary_profile(session, user.id)
        message = format_salary_profile(profile)
        await update.message.reply_text(message if profile is not None else f"{message}\n\n{usage}")
        return

    try:
        expected = Decimal(args[0])
    except InvalidOperation:
        await update.message.reply_text(usage)
        return
    if expected <= 0:
        await update.message.reply_text("Expected salary must be positive.")
        return

    savings_target: Decimal | None = None
    if len(args) >= 2:
        try:
            savings_target = Decimal(args[1])
        except InvalidOperation:
            await update.message.reply_text(usage)
            return
        if savings_target < 0 or savings_target > expected:
            await update.message.reply_text("Savings target must be between 0 and your expected salary.")
            return

    payday_day: int | None = None
    if len(args) >= 3:
        try:
            payday_day = int(args[2])
        except ValueError:
            await update.message.reply_text(usage)
            return
        if not 1 <= payday_day <= 28:
            await update.message.reply_text("Payday must be a day of the month between 1 and 28.")
            return

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        profile = await upsert_salary_profile(
            session, user.id, expected_salary=expected, savings_target=savings_target, payday_day=payday_day
        )

    await update.message.reply_text("✅ Salary profile saved.\n" + format_salary_profile(profile))


async def handle_setbudget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setbudget <category> <amount> — upserts a monthly limit. Category must
    match one of the CATEGORIES buttons (case-insensitively) so budgets and
    the ask_user flow always share one category vocabulary."""
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    args = context.args or []
    logger.info("chat=%s: /setbudget %r", chat_id, args)

    usage = "Usage: /setbudget <category> <amount>\ne.g. /setbudget Food 4000"
    if len(args) < 2:
        await update.message.reply_text(usage)
        return

    *category_words, amount_str = args
    category = normalize_category(" ".join(category_words))
    if category is None:
        valid = ", ".join(CATEGORIES.values())
        await update.message.reply_text(f"Unknown category — pick one of: {valid}")
        return

    try:
        amount = Decimal(amount_str)
    except InvalidOperation:
        await update.message.reply_text(usage)
        return
    if amount <= 0:
        await update.message.reply_text("Budget amount must be positive.")
        return

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        await upsert_budget(session, user.id, category, amount)

    await update.message.reply_text(f"Budget set: {category} — Rs.{amount:,.2f}/month")


async def handle_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/log <category> <amount> [notes] — manually log an expense with no
    underlying bank/UPI transaction at all (e.g. physical cash spend)."""
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    args = context.args or []
    logger.info("chat=%s: /log %r", chat_id, args)

    usage = "Usage: /log <category> <amount> [notes]\ne.g. /log Food 900 lunch with friends"
    if len(args) < 2:
        await update.message.reply_text(usage)
        return

    # Unlike /setbudget (which keeps asking, since a typo there would
    # silently redirect a monthly limit), an unrecognized /log category just
    # falls back to "Other" — the amount is never lost, and the fallback is
    # called out in the reply so it isn't a silent surprise later.
    category = normalize_category(args[0])
    category_note = ""
    if category is None:
        category_note = f" (unrecognized category {args[0]!r}, defaulted to Other)"
        category = CATEGORIES["other"]

    try:
        amount = Decimal(args[1])
    except InvalidOperation:
        await update.message.reply_text(usage)
        return
    if amount <= 0:
        await update.message.reply_text("Amount must be positive.")
        return

    notes = " ".join(args[2:]) or None

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        await log_manual_expense(session, user.id, category, amount, notes)

    note_suffix = f" ({notes})" if notes else ""
    await update.message.reply_text(f"Logged: Rs.{amount:,.2f} — {category}{note_suffix}{category_note}")


async def handle_budgets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/budgets — lists current limits and this month's spend per category."""
    if update.message is None:
        return
    chat_id = update.effective_chat.id
    logger.info("chat=%s: /budgets", chat_id)

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        statuses = await get_budget_statuses(session, user.id)

    await update.message.reply_text(format_budgets_message(statuses))


async def handle_ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ask <question> — same as just typing the question: the chat agent
    answers it with its tools (numbers always come from code, never the model)."""
    if update.message is None:
        return
    question = " ".join(context.args or []).strip()
    if not question:
        await update.message.reply_text(
            "Just type your question, e.g. how much did I spend on food this week? (or /ask <question>)"
        )
        return
    await answer_chat_message(update, context, question)


async def handle_plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/plan — this month so far: salary, savings moved, spending money, money left."""
    if update.message is None:
        return
    async with SessionLocal() as session:
        user = await _get_or_create_user(session, update.effective_chat.id)
        plan = await get_month_plan(session, user.id)
    await update.message.reply_text(plan.as_message())


async def handle_myaccount_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/myaccount savings <last 4 digits> — remember your own savings account,
    so transfers to it count as saving and money taken back isn't income.
    With no arguments, lists the accounts already saved."""
    if update.message is None:
        return
    args = context.args or []
    usage = (
        "Usage: /myaccount savings <last 4 digits> (your savings account)\n"
        "or /myaccount main <last 4 digits> (the account your salary comes into)"
    )

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, update.effective_chat.id)
        if not args:
            savings = await own_accounts(session, user.id, "savings")
            main = await own_accounts(session, user.id, "main")
            listed = [f"savings XX{last4}" for last4 in savings] + [f"main XX{last4}" for last4 in main]
            saved = ", ".join(listed) if listed else "none yet"
            await update.message.reply_text(f"Your accounts: {saved}\n{usage}")
            return

        kind = args[0].lower() if args else ""
        if len(args) != 2 or kind not in ("savings", "main") or not (len(args[1]) == 4 and args[1].isdigit()):
            await update.message.reply_text(usage)
            return
        added = await insert_once(session, OwnAccount(user_id=user.id, last4=args[1], kind=kind))

    if not added:
        await update.message.reply_text(f"XX{args[1]} is already saved.")
    elif kind == "savings":
        await update.message.reply_text(
            f"✅ Saved. Transfers to XX{args[1]} now count as savings, "
            "and money taken back from it isn't counted as income."
        )
    else:
        await update.message.reply_text(f"✅ Saved XX{args[1]} as your main account.")


async def handle_audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/audit — run the monthly salary audit for the requesting chat's user
    over the previous completed month, on demand (the same audit the
    1st-of-month cron runs for everyone). Idempotent per month, so a repeat
    call just recaps the already-computed result."""
    chat_id = update.effective_chat.id
    provider = context.bot_data["llm_provider"]
    logger.info("chat=%s: /audit requested", chat_id)

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        result = await run_monthly_audit(session, provider, user.id)

    logger.info("chat=%s: audit finished — status=%s questions=%d", chat_id, result.status, len(result.questions))

    if result.status == "no_data":
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Nothing to audit for {result.period_label} yet — no income or spending recorded.",
        )
        return

    if result.message:
        await context.bot.send_message(chat_id=chat_id, text=result.message)
    for question in result.questions:
        await send_audit_question(context.bot, chat_id, question)


async def handle_reconcile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reconcile — runs the agent loop for the requesting chat's user only."""
    chat_id = update.effective_chat.id
    provider = context.bot_data["llm_provider"]
    logger.info("chat=%s: /reconcile requested", chat_id)

    async with SessionLocal() as session:
        user = await _get_or_create_user(session, chat_id)
        summary = await reconcile(session, provider, user.id, trigger="reconcile")

    logger.info("chat=%s: reconcile finished — %s", chat_id, summary.as_message())

    for pending in summary.pending_questions:
        await send_ask_user_message(context.bot, chat_id, pending)

    await context.bot.send_message(chat_id=chat_id, text=summary.as_message())
