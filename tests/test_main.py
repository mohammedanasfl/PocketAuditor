"""Stage 5 tests: the FastAPI wiring layer (routes, secret-token check,
background-task scheduling). Handler/decision logic is already covered by
tests/test_agent.py and tests/test_parser.py — these tests only check that
app.routes routes requests correctly, without needing a real Telegram/network
round trip. TestClient(app) without a `with` block deliberately does not run
the app's lifespan (verified: app.state.application stays unset), which is
exactly what lets these tests avoid a live getMe() call to Telegram.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from telegram import Update

import app.main as main_module
import app.routes as routes_module
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


def test_webhook_rejects_everything_when_no_secret_configured(monkeypatch):
    # Fails closed: an unset secret must not mean "accept unauthenticated
    # requests" — that would let anyone who finds the URL inject forged
    # Telegram updates. See app/routes.py:telegram_webhook.
    monkeypatch.setattr(settings, "telegram_webhook_secret", None)

    class _StubApplication:
        bot = None

        async def process_update(self, update: Update) -> None:
            raise AssertionError("should never be reached when the secret is unset")

    monkeypatch.setattr(main_module.app.state, "application", _StubApplication(), raising=False)

    response = client.post(
        "/telegram/webhook",
        json={"update_id": 2},
        headers={"X-Telegram-Bot-Api-Secret-Token": "some-guess"},
    )
    assert response.status_code == 401


def test_reconcile_endpoint_returns_202_and_schedules_the_background_task(monkeypatch):
    calls: list = []

    async def _fake_reconcile_all_users(application) -> None:
        calls.append(application)

    stub_application = SimpleNamespace(bot_data={}, bot=None)
    monkeypatch.setattr(settings, "cron_secret", "expected-cron-secret")
    monkeypatch.setattr(main_module.app.state, "application", stub_application, raising=False)
    monkeypatch.setattr(routes_module, "reconcile_all_users", _fake_reconcile_all_users)

    response = client.post("/reconcile", headers={"X-Cron-Secret": "expected-cron-secret"})

    assert response.status_code == 202
    assert response.json() == {"status": "scheduled"}
    assert calls == [stub_application]


def test_reconcile_endpoint_rejects_wrong_or_missing_cron_secret(monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", "expected-cron-secret")

    assert client.post("/reconcile").status_code == 401
    assert client.post("/reconcile", headers={"X-Cron-Secret": "wrong"}).status_code == 401


def test_reconcile_endpoint_rejects_everything_when_no_cron_secret_configured(monkeypatch):
    # Same fail-closed reasoning as the webhook secret — an unset CRON_SECRET
    # must not mean "anyone who finds the URL can trigger a run for every user".
    monkeypatch.setattr(settings, "cron_secret", None)

    response = client.post("/reconcile", headers={"X-Cron-Secret": "anything"})
    assert response.status_code == 401


def test_check_budgets_endpoint_returns_202_and_schedules_the_background_task(monkeypatch):
    calls: list = []

    async def _fake_check_budgets_all_users(application) -> None:
        calls.append(application)

    stub_application = SimpleNamespace(bot_data={}, bot=None)
    monkeypatch.setattr(settings, "cron_secret", "expected-cron-secret")
    monkeypatch.setattr(main_module.app.state, "application", stub_application, raising=False)
    monkeypatch.setattr(routes_module, "check_budgets_all_users", _fake_check_budgets_all_users)

    response = client.post("/check-budgets", headers={"X-Cron-Secret": "expected-cron-secret"})

    assert response.status_code == 202
    assert response.json() == {"status": "scheduled"}
    assert calls == [stub_application]


def test_check_budgets_endpoint_rejects_wrong_or_missing_cron_secret(monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", "expected-cron-secret")

    assert client.post("/check-budgets").status_code == 401
    assert client.post("/check-budgets", headers={"X-Cron-Secret": "wrong"}).status_code == 401


def test_run_audit_endpoint_returns_202_and_schedules_the_background_task(monkeypatch):
    calls: list = []

    async def _fake_run_audit_all_users(application) -> None:
        calls.append(application)

    stub_application = SimpleNamespace(bot_data={}, bot=None)
    monkeypatch.setattr(settings, "cron_secret", "expected-cron-secret")
    monkeypatch.setattr(main_module.app.state, "application", stub_application, raising=False)
    monkeypatch.setattr(routes_module, "run_audit_all_users", _fake_run_audit_all_users)

    response = client.post("/run-audit", headers={"X-Cron-Secret": "expected-cron-secret"})

    assert response.status_code == 202
    assert response.json() == {"status": "scheduled"}
    assert calls == [stub_application]


def test_run_audit_endpoint_rejects_wrong_or_missing_cron_secret(monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", "expected-cron-secret")

    assert client.post("/run-audit").status_code == 401
    assert client.post("/run-audit", headers={"X-Cron-Secret": "wrong"}).status_code == 401


def test_check_salary_alerts_endpoint_returns_202_and_schedules_the_background_task(monkeypatch):
    calls: list = []

    async def _fake_check_salary_alerts_all_users(application) -> None:
        calls.append(application)

    stub_application = SimpleNamespace(bot_data={}, bot=None)
    monkeypatch.setattr(settings, "cron_secret", "expected-cron-secret")
    monkeypatch.setattr(main_module.app.state, "application", stub_application, raising=False)
    monkeypatch.setattr(routes_module, "check_salary_alerts_all_users", _fake_check_salary_alerts_all_users)

    response = client.post("/check-salary-alerts", headers={"X-Cron-Secret": "expected-cron-secret"})

    assert response.status_code == 202
    assert response.json() == {"status": "scheduled"}
    assert calls == [stub_application]


def test_check_salary_alerts_endpoint_rejects_wrong_or_missing_cron_secret(monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", "expected-cron-secret")

    assert client.post("/check-salary-alerts").status_code == 401
    assert client.post("/check-salary-alerts", headers={"X-Cron-Secret": "wrong"}).status_code == 401
