"""Registering the Telegram "/" command menu."""

from __future__ import annotations

from types import SimpleNamespace

from app.telegram.bot import BOT_COMMANDS, set_bot_commands


async def test_set_bot_commands_registers_the_command_list():
    calls = []

    async def fake_set_my_commands(commands):
        calls.append(commands)

    application = SimpleNamespace(bot=SimpleNamespace(set_my_commands=fake_set_my_commands))

    await set_bot_commands(application)

    assert calls == [BOT_COMMANDS]
    command_names = {c.command for c in BOT_COMMANDS}
    assert command_names == {
        "start",
        "help",
        "reconcile",
        "spend",
        "income",
        "salary",
        "audit",
        "log",
        "setbudget",
        "budgets",
        "ask",
    }
