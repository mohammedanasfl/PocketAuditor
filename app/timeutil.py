"""One place to ask "what is today's date?".

The server runs in UTC, but the user lives in India (UTC+5:30). Using
date.today() on the server gives the wrong day between midnight and 5:30 AM
IST — and the wrong month on the 1st. Every "today" in the app comes from
local_today() instead.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import settings


def local_today() -> date:
    """Today's date in the configured timezone (settings.timezone)."""
    return datetime.now(ZoneInfo(settings.timezone)).date()
