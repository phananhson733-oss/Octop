"""Keep credentials passed in URL query strings out of request logs.

Browsers cannot set headers on WebSocket, EventSource or media requests, so the dashboard
sends its JWT as ``?token=`` / ``?access_token=``, and uvicorn logs the full path of every
request and WebSocket handshake.
"""

from __future__ import annotations

import logging
import re

_QUERY_SECRET = re.compile(r"([?&][A-Za-z0-9_]*token=)[^&\s\"]*", re.IGNORECASE)
REQUEST_LOGGERS = ("uvicorn.access", "uvicorn.error")


def redact_query_secrets(text: str) -> str:
    """Replace the value of every ``…token=`` query parameter with ``***``."""
    return _QUERY_SECRET.sub(r"\1***", text)


class QuerySecretFilter(logging.Filter):
    """Masks ``…token=`` query values in a record's message and string arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_query_secrets(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_query_secrets(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True


def install_query_secret_filter() -> None:
    """Attach the filter to uvicorn's request loggers (idempotent).

    Logger filters survive uvicorn's ``dictConfig``, which only replaces handlers.
    """
    for name in REQUEST_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, QuerySecretFilter) for f in logger.filters):
            logger.addFilter(QuerySecretFilter())
