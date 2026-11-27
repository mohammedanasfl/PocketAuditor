"""Stage 2 tests: both LLMProvider implementations must produce identical,
schema-valid output from identical (mocked) model responses. This is the test
that actually enforces "the decision loop behaves identically regardless of
provider" — not just documents it.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.llm.base import LLMDecisionError, sanitize_schema_for_llm
from app.llm.claude import ClaudeProvider
from app.llm.gemini import GeminiProvider
from app.schemas import AuditReport, MatchDecision, QueryIntent

TRANSACTION = {"amount": "450.00", "merchant": "Blinkit", "txn_date": "2026-08-10"}
CANDIDATE_ID = str(uuid.uuid4())
CANDIDATES = [{"id": CANDIDATE_ID, "amount": "450.00", "merchant": "Blinkit", "txn_date": "2026-08-10"}]
ANOMALY_ID = str(uuid.uuid4())
SNAPSHOT = {
    "period": "July 2026",
    "total_income": "50000.00",
    "total_spend": "38000.00",
    "net_saved": "12000.00",
    "savings_rate_pct": "24.00",
    "anomaly_candidates": [{"id": ANOMALY_ID, "amount": "9000.00", "merchant": "unknown", "category": "Uncategorized"}],
}

# Stand-in bytes for a photo — extract_receipt tests mock the API response
# directly (same pattern as decide_match/parse_transaction above), so these
# never need to decode as a real image.
FAKE_IMAGE_BYTES = b"\xff\xd8\xff\xe0-fake-jpeg-bytes-for-testing"


def _decision_json(matched_id: str | None) -> str:
    return json.dumps(
        {
            "action": "auto_link" if matched_id else "auto_log",
            "matched_expense_id": matched_id,
            "suggested_category": None if matched_id else "Food",
            "confidence": 0.92,
            "reasoning": (
                "Amount, date, and merchant all match the candidate expense."
                if matched_id
                else "Clear grocery merchant and amount with no matching candidate."
            ),
        }
    )


def _extraction_json() -> str:
    return json.dumps(
        {
            "is_transaction": True,
            "amount": 450.0,
            "merchant": "Blinkit",
            "txn_date": "2026-08-10",
            "is_debit": True,
        }
    )


def _receipt_json(*, readable: bool, merchant: str | None = None, total_amount: float | None = None) -> str:
    return json.dumps(
        {
            "merchant": merchant,
            "total_amount": total_amount,
            "txn_date": "2026-08-14" if readable else None,
            "line_items": None,
            "confidence": 0.9 if readable else 0.15,
            "readable": readable,
        }
    )


def _query_intent_json(
    *,
    category: str | None = "Food",
    date_range: str = "this_week",
    aggregation: str = "sum",
    is_financial_question: bool = True,
) -> str:
    return json.dumps(
        {
            "is_financial_question": is_financial_question,
            "category": category,
            "date_range": date_range,
            "custom_start": None,
            "custom_end": None,
            "aggregation": aggregation,
            "intent_summary": "How much was spent on food this week.",
        }
    )


def _audit_report_json(flagged: list[str] | None = None) -> str:
    return json.dumps(
        {
            "summary": "You saved a healthy share of your income, though Food spending rose noticeably.",
            "recommendations": ["Consider setting a Food budget.", "Keep the savings momentum going."],
            "flagged_expense_ids": flagged if flagged is not None else [],
            "confidence": 0.88,
        }
    )


def _fake_claude_create(text: str):
    async def _create(**kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    return _create


def _fake_gemini_generate(text: str):
    async def _generate(**kwargs):
        return SimpleNamespace(text=text)

    return _generate


# --- schema sanitizer ------------------------------------------------------


def test_sanitize_schema_strips_unsupported_keys_but_keeps_contract():
    schema = MatchDecision.model_json_schema()
    raw = json.dumps(schema)
    assert "minimum" in raw and "maximum" in raw  # confidence's ge/le

    sanitized = sanitize_schema_for_llm(schema)
    sanitized_raw = json.dumps(sanitized)
    assert "minimum" not in sanitized_raw
    assert "maximum" not in sanitized_raw
    # the parts that matter for a strict schema are untouched
    assert sanitized["required"] == schema["required"]
    assert sanitized["additionalProperties"] is False


# --- ClaudeProvider ----------------------------------------------------------


async def test_claude_decide_match_valid_response(monkeypatch):
    provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(provider._client.messages, "create", _fake_claude_create(_decision_json(CANDIDATE_ID)))
    decision = await provider.decide_match(TRANSACTION, CANDIDATES)
    assert decision.action == "auto_link"
    assert str(decision.matched_expense_id) == CANDIDATE_ID


async def test_claude_decide_match_invalid_response_raises(monkeypatch):
    provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(provider._client.messages, "create", _fake_claude_create("not json"))
    with pytest.raises(LLMDecisionError):
        await provider.decide_match(TRANSACTION, CANDIDATES)


async def test_claude_parse_transaction_valid_response(monkeypatch):
    provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(provider._client.messages, "create", _fake_claude_create(_extraction_json()))
    extraction = await provider.parse_transaction("Rs.450 debited from A/c XX1234 at BLINKIT on 10-08-26")
    assert extraction.amount == 450.0
    assert extraction.is_debit is True


async def test_claude_extract_receipt_clear_image_returns_readable(monkeypatch):
    provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(
        provider._client.messages,
        "create",
        _fake_claude_create(_receipt_json(readable=True, merchant="Reliance Fresh", total_amount=450.0)),
    )
    receipt = await provider.extract_receipt(FAKE_IMAGE_BYTES, "image/jpeg")
    assert receipt.readable is True
    assert receipt.merchant == "Reliance Fresh"
    assert receipt.total_amount == 450.0


async def test_claude_extract_receipt_blurry_image_returns_unreadable(monkeypatch):
    provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(provider._client.messages, "create", _fake_claude_create(_receipt_json(readable=False)))
    receipt = await provider.extract_receipt(FAKE_IMAGE_BYTES, "image/jpeg")
    assert receipt.readable is False
    assert receipt.total_amount is None  # must not hallucinate a number


async def test_claude_extract_receipt_non_receipt_image_returns_unreadable(monkeypatch):
    provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(provider._client.messages, "create", _fake_claude_create(_receipt_json(readable=False)))
    receipt = await provider.extract_receipt(FAKE_IMAGE_BYTES, "image/png")
    assert receipt.readable is False


async def test_claude_extract_receipt_invalid_response_raises(monkeypatch):
    provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(provider._client.messages, "create", _fake_claude_create("not json"))
    with pytest.raises(LLMDecisionError):
        await provider.extract_receipt(FAKE_IMAGE_BYTES, "image/jpeg")


async def test_claude_interpret_query_valid_response(monkeypatch):
    provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(provider._client.messages, "create", _fake_claude_create(_query_intent_json()))
    intent = await provider.interpret_query("how much did I spend on food this week")
    assert intent.category == "Food"
    assert intent.date_range == "this_week"
    assert intent.aggregation == "sum"


async def test_claude_interpret_query_invalid_response_raises(monkeypatch):
    provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(provider._client.messages, "create", _fake_claude_create("not json"))
    with pytest.raises(LLMDecisionError):
        await provider.interpret_query("nonsense question")


# --- GeminiProvider ----------------------------------------------------------


async def test_gemini_decide_match_valid_response(monkeypatch):
    provider = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(
        provider._client.aio.models, "generate_content", _fake_gemini_generate(_decision_json(CANDIDATE_ID))
    )
    decision = await provider.decide_match(TRANSACTION, CANDIDATES)
    assert decision.action == "auto_link"
    assert str(decision.matched_expense_id) == CANDIDATE_ID


async def test_gemini_decide_match_invalid_response_raises(monkeypatch):
    provider = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(provider._client.aio.models, "generate_content", _fake_gemini_generate("not json"))
    with pytest.raises(LLMDecisionError):
        await provider.decide_match(TRANSACTION, CANDIDATES)


async def test_gemini_parse_transaction_valid_response(monkeypatch):
    provider = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(provider._client.aio.models, "generate_content", _fake_gemini_generate(_extraction_json()))
    extraction = await provider.parse_transaction("Rs.450 debited from A/c XX1234 at BLINKIT on 10-08-26")
    assert extraction.amount == 450.0
    assert extraction.is_debit is True


async def test_gemini_extract_receipt_clear_image_returns_readable(monkeypatch):
    provider = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(
        provider._client.aio.models,
        "generate_content",
        _fake_gemini_generate(_receipt_json(readable=True, merchant="Reliance Fresh", total_amount=450.0)),
    )
    receipt = await provider.extract_receipt(FAKE_IMAGE_BYTES, "image/jpeg")
    assert receipt.readable is True
    assert receipt.merchant == "Reliance Fresh"
    assert receipt.total_amount == 450.0


async def test_gemini_extract_receipt_blurry_image_returns_unreadable(monkeypatch):
    provider = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(
        provider._client.aio.models, "generate_content", _fake_gemini_generate(_receipt_json(readable=False))
    )
    receipt = await provider.extract_receipt(FAKE_IMAGE_BYTES, "image/jpeg")
    assert receipt.readable is False
    assert receipt.total_amount is None


async def test_gemini_extract_receipt_invalid_response_raises(monkeypatch):
    provider = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(provider._client.aio.models, "generate_content", _fake_gemini_generate("not json"))
    with pytest.raises(LLMDecisionError):
        await provider.extract_receipt(FAKE_IMAGE_BYTES, "image/jpeg")


async def test_gemini_interpret_query_valid_response(monkeypatch):
    provider = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(provider._client.aio.models, "generate_content", _fake_gemini_generate(_query_intent_json()))
    intent = await provider.interpret_query("how much did I spend on food this week")
    assert intent.category == "Food"
    assert intent.date_range == "this_week"
    assert intent.aggregation == "sum"


async def test_gemini_interpret_query_invalid_response_raises(monkeypatch):
    provider = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(provider._client.aio.models, "generate_content", _fake_gemini_generate("not json"))
    with pytest.raises(LLMDecisionError):
        await provider.interpret_query("nonsense question")


# --- parity: the brief's core requirement -----------------------------------


async def test_decide_match_identical_across_providers(monkeypatch):
    """Feed both providers byte-equivalent model output and assert the
    resulting MatchDecision objects are equal — this is what makes the
    provider swap actually safe, not just documented as safe."""
    decision_json = _decision_json(CANDIDATE_ID)

    claude = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(claude._client.messages, "create", _fake_claude_create(decision_json))
    claude_decision = await claude.decide_match(TRANSACTION, CANDIDATES)

    gemini = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(gemini._client.aio.models, "generate_content", _fake_gemini_generate(decision_json))
    gemini_decision = await gemini.decide_match(TRANSACTION, CANDIDATES)

    assert claude_decision == gemini_decision


async def test_parse_transaction_identical_across_providers(monkeypatch):
    extraction_json = _extraction_json()
    raw_sms = "Rs.450 debited from A/c XX1234 at BLINKIT on 10-08-26"

    claude = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(claude._client.messages, "create", _fake_claude_create(extraction_json))
    claude_extraction = await claude.parse_transaction(raw_sms)

    gemini = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(gemini._client.aio.models, "generate_content", _fake_gemini_generate(extraction_json))
    gemini_extraction = await gemini.parse_transaction(raw_sms)

    assert claude_extraction == gemini_extraction


async def test_interpret_query_identical_across_providers(monkeypatch):
    intent_json = _query_intent_json()

    claude = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(claude._client.messages, "create", _fake_claude_create(intent_json))
    claude_intent = await claude.interpret_query("how much on food this week")

    gemini = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(gemini._client.aio.models, "generate_content", _fake_gemini_generate(intent_json))
    gemini_intent = await gemini.interpret_query("how much on food this week")

    assert claude_intent == gemini_intent
    assert isinstance(claude_intent, QueryIntent)


# --- audit_finances (Phase 4) -----------------------------------------------


async def test_claude_audit_finances_valid_response(monkeypatch):
    provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(provider._client.messages, "create", _fake_claude_create(_audit_report_json([ANOMALY_ID])))
    report = await provider.audit_finances(SNAPSHOT)
    assert [str(i) for i in report.flagged_expense_ids] == [ANOMALY_ID]


async def test_claude_audit_finances_invalid_response_raises(monkeypatch):
    provider = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(provider._client.messages, "create", _fake_claude_create("not json"))
    with pytest.raises(LLMDecisionError):
        await provider.audit_finances(SNAPSHOT)


async def test_gemini_audit_finances_valid_response(monkeypatch):
    provider = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(
        provider._client.aio.models, "generate_content", _fake_gemini_generate(_audit_report_json([ANOMALY_ID]))
    )
    report = await provider.audit_finances(SNAPSHOT)
    assert [str(i) for i in report.flagged_expense_ids] == [ANOMALY_ID]


async def test_gemini_audit_finances_invalid_response_raises(monkeypatch):
    provider = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(provider._client.aio.models, "generate_content", _fake_gemini_generate("not json"))
    with pytest.raises(LLMDecisionError):
        await provider.audit_finances(SNAPSHOT)


async def test_audit_finances_identical_across_providers(monkeypatch):
    report_json = _audit_report_json([ANOMALY_ID])

    claude = ClaudeProvider(api_key="test-key", model="claude-sonnet-5")
    monkeypatch.setattr(claude._client.messages, "create", _fake_claude_create(report_json))
    claude_report = await claude.audit_finances(SNAPSHOT)

    gemini = GeminiProvider(api_key="test-key", model="gemini-flash-lite-latest")
    monkeypatch.setattr(gemini._client.aio.models, "generate_content", _fake_gemini_generate(report_json))
    gemini_report = await gemini.audit_finances(SNAPSHOT)

    assert claude_report == gemini_report
    assert isinstance(claude_report, AuditReport)
