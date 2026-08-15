"""Central logging setup.

Uvicorn only configures its own "uvicorn" / "uvicorn.access" / "uvicorn.error"
loggers (each with propagate=False) — it never attaches a handler to the root
logger. Without this, every logger.info() call anywhere in app/* silently
disappears: the record propagates up to root, finds no handler there, and
Python's logging module drops it (root's "handler of last resort" only
surfaces WARNING and above). This is why nothing but uvicorn's own request
lines showed up before.

Call configure_logging() once, as early as possible — app/main.py does this
at module level, before the FastAPI app or the Telegram Application exist.
"""

from __future__ import annotations

import logging

_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,  # override anything a library set up before us
    )

    # Noisy at INFO (every HTTP call, every long-poll tick) and would drown
    # out our own app.* logs.
    for noisy_logger in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    _configured = True
