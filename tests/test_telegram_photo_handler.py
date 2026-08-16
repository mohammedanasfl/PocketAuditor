"""Stage 2 tests: the photo message handler. Verifies it creates a
source='photo' transaction only when ExtractedReceipt is trustworthy
(readable and above the confidence threshold), and degrades gracefully
(no transaction, a friendly reply) otherwise — including when the provider
itself can't do vision (NotImplementedError) or returns a schema-invalid
response (LLMDecisionError). Whether that transaction then flows through
reconcile_user identically to an SMS one is Stage 3's test.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.telegram.handlers.messages as handlers_module
from app.db import Base
from app.llm.base import LLMDecisionError
from app.models import Transaction
from app.schemas import ExtractedReceipt
from app.telegram.handlers import handle_photo_message


class _FakeProvider:
    def __init__(self, receipt: ExtractedReceipt | None = None, error: Exception | None = None):
        self._receipt = receipt
        self._error = error

    async def extract_receipt(self, image_bytes: bytes, mime_type: str) -> ExtractedReceipt:
        if self._error is not None:
            raise self._error
        assert self._receipt is not None
        return self._receipt


class _FakeFile:
    async def download_as_bytearray(self) -> bytearray:
        return bytearray(b"fake-jpeg-bytes")


class _FakeBot:
    async def get_file(self, file_id):
        return _FakeFile()


def _make_update(caption: str | None = None):
    replies: list[str] = []

    async def reply_text(text: str) -> None:
        replies.append(text)

    message = SimpleNamespace(photo=[SimpleNamespace(file_id="abc123")], caption=caption, reply_text=reply_text)
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=555))
    return update, replies


async def _all_transactions(session_factory) -> list[Transaction]:
    async with session_factory() as session:
        return list((await session.execute(select(Transaction))).scalars().all())


async def _sqlite_session_factory(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'photo.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(handlers_module, "SessionLocal", session_factory)
    return session_factory, engine


async def test_readable_receipt_creates_photo_transaction(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, replies = _make_update()
    receipt = ExtractedReceipt(
        merchant="Reliance Fresh",
        total_amount=450.0,
        txn_date=date(2026, 8, 14),
        line_items=None,
        confidence=0.9,
        readable=True,
    )
    context = SimpleNamespace(bot_data={"llm_provider": _FakeProvider(receipt=receipt)}, bot=_FakeBot())

    await handle_photo_message(update, context)

    transactions = await _all_transactions(session_factory)
    assert len(transactions) == 1
    txn = transactions[0]
    assert txn.source == "photo"
    assert txn.status == "pending"
    assert txn.merchant == "Reliance Fresh"
    assert txn.txn_date == date(2026, 8, 14)
    assert txn.amount == Decimal("450.0")

    assert len(replies) == 1
    assert "450.0" in replies[0] and "Reliance Fresh" in replies[0]
    await engine.dispose()


async def test_photo_caption_sets_category_hint(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, replies = _make_update(caption="Food")
    receipt = ExtractedReceipt(
        merchant="Reliance Fresh",
        total_amount=450.0,
        txn_date=date(2026, 8, 14),
        line_items=None,
        confidence=0.9,
        readable=True,
    )
    context = SimpleNamespace(bot_data={"llm_provider": _FakeProvider(receipt=receipt)}, bot=_FakeBot())

    await handle_photo_message(update, context)

    txn = (await _all_transactions(session_factory))[0]
    assert txn.category_hint == "Food"
    assert "categorized as Food" in replies[0]
    await engine.dispose()


async def test_photo_caption_is_case_insensitive_against_known_categories(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, replies = _make_update(caption="food")  # lowercase
    receipt = ExtractedReceipt(
        merchant="Reliance Fresh",
        total_amount=450.0,
        txn_date=date(2026, 8, 14),
        line_items=None,
        confidence=0.9,
        readable=True,
    )
    context = SimpleNamespace(bot_data={"llm_provider": _FakeProvider(receipt=receipt)}, bot=_FakeBot())

    await handle_photo_message(update, context)

    txn = (await _all_transactions(session_factory))[0]
    assert txn.category_hint == "Food"  # normalized to canonical casing
    await engine.dispose()


async def test_photo_unrecognized_caption_leaves_category_hint_null(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, replies = _make_update(caption="lunch with friends")  # not one of CATEGORIES
    receipt = ExtractedReceipt(
        merchant="Reliance Fresh",
        total_amount=450.0,
        txn_date=date(2026, 8, 14),
        line_items=None,
        confidence=0.9,
        readable=True,
    )
    context = SimpleNamespace(bot_data={"llm_provider": _FakeProvider(receipt=receipt)}, bot=_FakeBot())

    await handle_photo_message(update, context)

    txn = (await _all_transactions(session_factory))[0]
    assert txn.category_hint is None
    assert "categorized as" not in replies[0]
    await engine.dispose()


async def test_photo_with_no_caption_leaves_category_hint_null(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, replies = _make_update()  # no caption
    receipt = ExtractedReceipt(
        merchant="Reliance Fresh",
        total_amount=450.0,
        txn_date=date(2026, 8, 14),
        line_items=None,
        confidence=0.9,
        readable=True,
    )
    context = SimpleNamespace(bot_data={"llm_provider": _FakeProvider(receipt=receipt)}, bot=_FakeBot())

    await handle_photo_message(update, context)

    txn = (await _all_transactions(session_factory))[0]
    assert txn.category_hint is None
    await engine.dispose()


async def test_unreadable_receipt_creates_no_transaction_and_asks_retake(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, replies = _make_update()
    receipt = ExtractedReceipt(
        merchant=None, total_amount=None, txn_date=None, line_items=None, confidence=0.15, readable=False
    )
    context = SimpleNamespace(bot_data={"llm_provider": _FakeProvider(receipt=receipt)}, bot=_FakeBot())

    await handle_photo_message(update, context)

    assert await _all_transactions(session_factory) == []
    assert len(replies) == 1
    assert "retake" in replies[0].lower() or "manually" in replies[0].lower()
    await engine.dispose()


async def test_low_confidence_receipt_creates_no_transaction_even_if_readable(tmp_path, monkeypatch):
    """readable=True alone isn't enough — a below-threshold confidence must
    also be treated as untrustworthy, same guard philosophy as agent.py's
    _apply_guard for decide_match."""
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, replies = _make_update()
    receipt = ExtractedReceipt(
        merchant="Some Shop",
        total_amount=100.0,
        txn_date=date(2026, 8, 14),
        line_items=None,
        confidence=0.5,
        readable=True,
    )
    context = SimpleNamespace(bot_data={"llm_provider": _FakeProvider(receipt=receipt)}, bot=_FakeBot())

    await handle_photo_message(update, context)

    assert await _all_transactions(session_factory) == []
    assert len(replies) == 1
    await engine.dispose()


async def test_provider_not_implemented_degrades_gracefully(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, replies = _make_update()
    context = SimpleNamespace(
        bot_data={"llm_provider": _FakeProvider(error=NotImplementedError("no vision model"))},
        bot=_FakeBot(),
    )

    await handle_photo_message(update, context)

    assert await _all_transactions(session_factory) == []
    assert len(replies) == 1
    await engine.dispose()


async def test_provider_llm_decision_error_degrades_gracefully(tmp_path, monkeypatch):
    session_factory, engine = await _sqlite_session_factory(tmp_path, monkeypatch)
    update, replies = _make_update()
    context = SimpleNamespace(
        bot_data={"llm_provider": _FakeProvider(error=LLMDecisionError("bad json"))},
        bot=_FakeBot(),
    )

    await handle_photo_message(update, context)

    assert await _all_transactions(session_factory) == []
    assert len(replies) == 1
    await engine.dispose()
