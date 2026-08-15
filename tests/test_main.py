"""Stage 5 tests: the FastAPI wiring layer (routes, secret-token check,
background-task scheduling). Handler/decision logic is already covered by
tests/test_agent.py and tests/test_parser.py — these tests only check that
main.py routes requests correctly, without needing a real Telegram/network
round trip. TestClient(app) without a `with` block deliberately does not run
the app's lifespan (verified: app.state.application stays unset), which is
exactly what lets these tests avoid a live getMe() call to Telegram.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from telegram import Update

import app.main as main_module
from app.config import settings

client = TestClient(main_module.app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_webhook_rejects_invalid_secret_token(monkeypatch):
    monkeypatch.setattr(settings, "telegram_webhook_secret", "expected-secret")
    response = client.post(
        "/telegram/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )
    assert response.status_code == 401


def test_webhook_accepts_valid_secret_and_forwards_the_update(monkeypatch):
    monkeypatch.setattr(settings, "telegram_webhook_secret", "expected-secret")

    processed: list[Update] = []

    class _StubApplication:
        bot = None

        async def process_update(self, update: Update) -> None:
            processed.append(update)

    monkeypatch.setattr(main_module.app.state, "application", _StubApplication(), raising=False)

    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1690000000,
            "chat": {"id": 123, "type": "private"},
            "text": "hello",
        },
    }
    response = client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(processed) == 1
    assert processed[0].update_id == 1
    assert processed[0].message.text == "hello"


def test_webhook_allows_missing_header_when_no_secret_configured(monkeypatch):
    monkeypatch.setattr(settings, "telegram_webhook_secret", None)

    class _StubApplication:
        bot = None

        async def process_update(self, update: Update) -> None:
            pass

    monkeypatch.setattr(main_module.app.state, "application", _StubApplication(), raising=False)

    response = client.post("/telegram/webhook", json={"update_id": 2})
    assert response.status_code == 200


def test_reconcile_endpoint_returns_202_and_schedules_the_background_task(monkeypatch):
    calls: list = []

    async def _fake_run_reconcile_all_users(application) -> None:
        calls.append(application)

    stub_application = SimpleNamespace(bot_data={}, bot=None)
    monkeypatch.setattr(main_module.app.state, "application", stub_application, raising=False)
    monkeypatch.setattr(main_module, "_run_reconcile_all_users", _fake_run_reconcile_all_users)

    response = client.post("/reconcile")

    assert response.status_code == 202
    assert response.json() == {"status": "scheduled"}
    assert calls == [stub_application]


def test_check_budgets_endpoint_returns_202_and_schedules_the_background_task(monkeypatch):
    calls: list = []

    async def _fake_run_check_budgets_all_users(application) -> None:
        calls.append(application)

    stub_application = SimpleNamespace(bot_data={}, bot=None)
    monkeypatch.setattr(main_module.app.state, "application", stub_application, raising=False)
    monkeypatch.setattr(main_module, "_run_check_budgets_all_users", _fake_run_check_budgets_all_users)

    response = client.post("/check-budgets")

    assert response.status_code == 202
    assert response.json() == {"status": "scheduled"}
    assert calls == [stub_application]
