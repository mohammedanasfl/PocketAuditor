"""Deployment wiring: the webhook registers itself on startup (Render), and
the public URL is read from PUBLIC_URL or Render's RENDER_EXTERNAL_URL."""

from __future__ import annotations

from types import SimpleNamespace

from app.config import Settings, settings
from app.main import register_webhook


class _FakeBot:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def set_webhook(self, url: str, secret_token: str) -> None:
        self.calls.append({"url": url, "secret_token": secret_token})


async def test_webhook_is_registered_on_startup(monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://pocketauditor.onrender.com/")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
    bot = _FakeBot()

    await register_webhook(SimpleNamespace(bot=bot))

    assert bot.calls == [{"url": "https://pocketauditor.onrender.com/telegram/webhook", "secret_token": "s3cret"}]


async def test_webhook_is_not_registered_without_a_secret(monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://pocketauditor.onrender.com")
    monkeypatch.setattr(settings, "telegram_webhook_secret", None)
    bot = _FakeBot()

    await register_webhook(SimpleNamespace(bot=bot))

    assert bot.calls == []


def test_public_url_comes_from_render_automatically(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
    assert Settings(database_url="d", telegram_bot_token="t").public_url == "https://x.onrender.com"


def test_explicit_public_url_wins(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://my.domain")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
    assert Settings(database_url="d", telegram_bot_token="t").public_url == "https://my.domain"
