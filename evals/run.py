"""Eval runner.

    python -m evals.run --suite parser    --provider none
    python -m evals.run --suite reconcile --provider gemini --engine pipeline
    python -m evals.run --suite reconcile --provider gemini --engine agent

Each reconcile scenario gets its own fresh SQLite database, so scenarios
never affect each other. Results are printed and written to
evals/results/<suite>-<engine>-<provider>.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import models  # noqa: F401  (registers all tables on Base.metadata)
from app.db import Base
from app.models import Expense, Income, MerchantCategory, OwnAccount, ReconciliationRun, Transaction, User
from app.parser import ParseError, parse_sms
from app.savings import SAVINGS_WITHDRAWAL
from evals.engines import ENGINES
from evals.providers import CountingProvider, make_provider

SCENARIO_DIR = Path(__file__).parent / "scenarios"
RESULTS_DIR = Path(__file__).parent / "results"


@dataclass
class ScenarioResult:
    scenario_id: str
    expected: str
    got: str
    correct: bool
    unsafe: bool = False
    unnecessary_ask: bool = False
    llm_calls: int = 0
    seconds: float = 0.0
    tokens: int = 0


def load_scenarios(suite: str) -> list[dict]:
    scenarios = []
    with open(SCENARIO_DIR / f"{suite}.jsonl", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                scenarios.append(json.loads(line))
    return scenarios


# --- parser suite ---------------------------------------------------------


async def run_parser_scenario(scenario: dict, provider: CountingProvider) -> ScenarioResult:
    expected = scenario["expected"]
    try:
        parsed = await parse_sms(scenario["sms"], provider)
    except ParseError:
        parsed = None

    if parsed is None:
        got = "not a transaction"
        correct = expected["is_transaction"] is False
    else:
        direction = "debit" if parsed.is_debit else "credit"
        got = f"{direction} {parsed.amount} {parsed.merchant or '-'}"
        correct = expected["is_transaction"] is True
        if correct:
            correct = parsed.is_debit == expected["is_debit"] and parsed.amount == Decimal(expected["amount"])
        if correct and expected.get("merchant"):
            correct = expected["merchant"].lower() in (parsed.merchant or "").lower()

    if expected["is_transaction"]:
        want = f"{'debit' if expected['is_debit'] else 'credit'} {expected['amount']} {expected.get('merchant', '')}"
    else:
        want = "not a transaction"
    # Unsafe = wrong money data would be saved: a non-transaction saved as
    # one, or a real one saved with the wrong amount or direction. (A wrong
    # merchant name alone is not counted as unsafe.)
    unsafe = False
    if parsed is not None:
        if expected["is_transaction"] is False:
            unsafe = True
        elif parsed.is_debit != expected["is_debit"] or parsed.amount != Decimal(expected["amount"]):
            unsafe = True
    return ScenarioResult(scenario["id"], want.strip(), got, correct, unsafe=unsafe)


# --- reconcile suite ------------------------------------------------------


async def seed(session: AsyncSession, user: User, setup: dict) -> None:
    for row in setup.get("expenses", []):
        expense = Expense(
            user_id=user.id,
            amount=Decimal(row["amount"]),
            category=row["category"],
            merchant=row.get("merchant"),
            notes=row.get("notes"),
            txn_date=date.fromisoformat(row["txn_date"]),
            created_via="manual",
        )
        if row.get("linked"):
            # An expense that already came from an earlier SMS.
            old_txn = Transaction(
                user_id=user.id,
                raw_text="(earlier sms)",
                amount=expense.amount,
                merchant=expense.merchant,
                txn_date=expense.txn_date,
                status="processed",
            )
            session.add(old_txn)
            await session.flush()
            expense.linked_transaction_id = old_txn.id
            expense.created_via = "auto_log"
        session.add(expense)
    for last4 in setup.get("own_savings_accounts", []):
        session.add(OwnAccount(user_id=user.id, last4=last4, kind="savings"))
    for row in setup.get("memory", []):
        session.add(MerchantCategory(user_id=user.id, merchant=row["merchant"], category=row["category"]))
    await session.commit()


async def describe_outcome(
    session: AsyncSession, user: User, seeded_expense_ids: set
) -> tuple[str, str | None, Decimal | None]:
    """Look at the database and say what happened: (outcome, category, amount)."""
    incomes = (await session.execute(select(Income).where(Income.user_id == user.id))).scalars().all()
    txns = (
        (
            await session.execute(
                select(Transaction).where(Transaction.user_id == user.id, Transaction.raw_text != "(earlier sms)")
            )
        )
        .scalars()
        .all()
    )
    if incomes and not txns:
        outcome = "savings_withdrawal" if incomes[0].kind == SAVINGS_WITHDRAWAL else "income"
        return outcome, None, incomes[0].amount
    if not txns:
        return "rejected", None, None

    txn = txns[0]
    linked = (await session.execute(select(Expense).where(Expense.linked_transaction_id == txn.id))).scalars().all()
    for expense in linked:
        if expense.id in seeded_expense_ids:
            return "auto_link", expense.category, txn.amount
        if expense.merchant == "Own savings account":
            return "savings_transfer", expense.category, txn.amount
        return "auto_log", expense.category, txn.amount
    if txn.status == "pending" and await _has_open_question(session, user):
        return "ask_user", None, txn.amount
    return "error", None, txn.amount


async def _has_open_question(session: AsyncSession, user: User) -> bool:
    """True if the user has been asked something they haven't answered yet."""
    open_runs = await session.execute(
        select(ReconciliationRun.id).where(ReconciliationRun.user_id == user.id, ReconciliationRun.status == "open")
    )
    return open_runs.first() is not None


async def run_reconcile_scenario(
    scenario: dict, provider: CountingProvider, engine_name: str, workdir: Path
) -> ScenarioResult:
    engine = create_async_engine(f"sqlite+aiosqlite:///{workdir / (scenario['id'] + '.db')}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            user = User(telegram_chat_id=1)
            session.add(user)
            await session.commit()
            await seed(session, user, scenario.get("setup", {}))
            seeded_ids = set((await session.execute(select(Expense.id))).scalars().all())

            process_sms = ENGINES[engine_name]
            try:
                await process_sms(session, user, scenario["sms"], provider)
            except Exception as exc:  # an eval must record a crash, not stop
                print(f"  ! {scenario['id']} crashed: {exc!r}")
            outcome, category, amount = await describe_outcome(session, user, seeded_ids)
    finally:
        await engine.dispose()

    expected = scenario["expected"]
    good_categories = [expected.get("category")] + expected.get("also_ok", [])
    correct = outcome == expected["outcome"]
    if correct and outcome == "auto_log" and expected.get("category"):
        correct = category in good_categories
    if correct and expected.get("amount"):
        correct = amount == Decimal(expected["amount"])

    wrote_expense = outcome in ("auto_log", "auto_link")
    should_not_write = expected["outcome"] in ("ask_user", "rejected", "income", "savings_withdrawal")
    wrote_expense = wrote_expense or outcome == "savings_transfer"
    unsafe = (
        (wrote_expense and should_not_write)
        or (outcome == "auto_log" and expected.get("category") is not None and category not in good_categories)
        or (category is not None and category == expected.get("forbidden_category"))
        or (outcome == "auto_link" and expected["outcome"] == "auto_log")
        or (outcome in ("income", "savings_withdrawal") and expected["outcome"] != outcome)
        or (outcome == "savings_transfer" and expected["outcome"] != "savings_transfer")
    )
    unnecessary_ask = outcome == "ask_user" and expected["outcome"] in ("auto_log", "auto_link")
    want = expected["outcome"] + (f" {expected['category']}" if expected.get("category") else "")
    got = outcome + (f" {category}" if category and outcome == "auto_log" else "")
    return ScenarioResult(scenario["id"], want, got, correct, unsafe=unsafe, unnecessary_ask=unnecessary_ask)


# --- report ---------------------------------------------------------------


def write_report(suite: str, engine: str, provider_name: str, model: str | None, results: list[ScenarioResult]) -> str:
    total = len(results)
    correct = sum(1 for r in results if r.correct)
    unsafe = sum(1 for r in results if r.unsafe)
    asks = sum(1 for r in results if r.unnecessary_ask)
    calls = sum(r.llm_calls for r in results)
    seconds = sum(r.seconds for r in results)
    tokens = sum(r.tokens for r in results)

    lines = [
        f"# Eval: {suite} — engine={engine}, provider={provider_name}" + (f" ({model})" if model else ""),
        "",
        f"Run on {date.today().isoformat()}.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Correct | {correct}/{total} ({100 * correct / total:.0f}%) |",
        f"| Unsafe (wrong data written) | {unsafe} |",
        f"| Unnecessary asks | {asks} |",
        f"| LLM calls | {calls} ({calls / total:.1f} per scenario) |",
        f"| LLM time | {seconds:.1f}s ({seconds / total:.2f}s per scenario) |",
        f"| Agent tokens | {tokens} ({tokens / total:.0f} per scenario) |",
        "",
        "| Scenario | Expected | Got | Result |",
        "|---|---|---|---|",
    ]
    for r in results:
        mark = "✅" if r.correct else ("❌ unsafe" if r.unsafe else ("⚠️ asked" if r.unnecessary_ask else "❌"))
        expected = r.expected.replace("|", "/")
        got = r.got.replace("|", "/").replace("\n", " ")
        lines.append(f"| {r.scenario_id} | {expected} | {got} | {mark} |")
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run an eval suite.")
    parser.add_argument("--suite", choices=["parser", "reconcile"], required=True)
    parser.add_argument("--provider", default="none", help="none | gemini | claude")
    parser.add_argument("--model", default=None, help="model id (defaults to LLM_MODEL from .env)")
    parser.add_argument("--engine", default="pipeline", choices=sorted(ENGINES))
    parser.add_argument(
        "--fail-on-unsafe", action="store_true", help="exit with an error if any scenario wrote wrong data (for CI)"
    )
    args = parser.parse_args()

    provider = CountingProvider(make_provider(args.provider, args.model))
    scenarios = load_scenarios(args.suite)
    results: list[ScenarioResult] = []

    with tempfile.TemporaryDirectory() as tmp:
        for scenario in scenarios:
            provider.reset()
            started = time.perf_counter()
            if args.suite == "parser":
                result = await run_parser_scenario(scenario, provider)
            else:
                result = await run_reconcile_scenario(scenario, provider, args.engine, Path(tmp))
            result.llm_calls = provider.calls
            result.seconds = provider.seconds
            result.tokens = provider.input_tokens + provider.output_tokens
            results.append(result)
            print(
                f"{'PASS' if result.correct else 'FAIL'}  {result.scenario_id}  ({time.perf_counter() - started:.1f}s)"
            )

    engine_label = "regex" if args.suite == "parser" else args.engine
    report = write_report(args.suite, engine_label, args.provider, args.model, results)
    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"{args.suite}-{engine_label}-{args.provider}.md"
    out_path.write_text(report, encoding="utf-8")
    print()
    print(report)
    print(f"Saved to {out_path}")

    unsafe = [r.scenario_id for r in results if r.unsafe]
    if args.fail_on_unsafe and unsafe:
        raise SystemExit(f"Unsafe results: {', '.join(unsafe)}")


if __name__ == "__main__":
    asyncio.run(main())
