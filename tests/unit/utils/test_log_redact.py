"""tests/unit/utils/test_log_redact.py"""

from __future__ import annotations

import logging

import pytest
import uvicorn

from octop.infra.utils.log_redact import (
    REQUEST_LOGGERS,
    QuerySecretFilter,
    install_query_secret_filter,
    redact_query_secrets,
)


def test_token_query_values_are_masked() -> None:
    assert (
        redact_query_secrets("/api/notifications/ws?token=eyJhbGci.payload.sig")
        == "/api/notifications/ws?token=***"
    )
    assert (
        redact_query_secrets("/api/files/raw?path=a.mp4&access_token=abc&x=1")
        == "/api/files/raw?path=a.mp4&access_token=***&x=1"
    )
    assert redact_query_secrets("/a?Refresh_Token=abc") == "/a?Refresh_Token=***"


def test_other_text_is_left_alone() -> None:
    for text in ("/api/agents?limit=5", "token=abc without a query", "/a?tokens=1"):
        assert redact_query_secrets(text) == text


@pytest.fixture
def clean_request_loggers():
    yield
    for name in REQUEST_LOGGERS:
        logger = logging.getLogger(name)
        for f in [f for f in logger.filters if isinstance(f, QuerySecretFilter)]:
            logger.removeFilter(f)


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def test_uvicorn_lines_are_masked_after_its_log_config(clean_request_loggers) -> None:
    install_query_secret_filter()
    install_query_secret_filter()
    # uvicorn re-applies its dictConfig for every Config; the filter must survive that.
    uvicorn.Config(app=None)
    collect = _Collect()
    loggers = [logging.getLogger(name) for name in REQUEST_LOGGERS]
    for logger in loggers:
        assert sum(isinstance(f, QuerySecretFilter) for f in logger.filters) == 1
        logger.addHandler(collect)
    try:
        loggers[1].info('%s - "WebSocket %s" [accepted]', "1.2.3.4:5", "/api/chat/ws?token=secret")
        loggers[0].info(
            '%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/raw?access_token=secret", "1.1", 200
        )
    finally:
        for logger in loggers:
            logger.removeHandler(collect)
    assert collect.lines == [
        '1.2.3.4:5 - "WebSocket /api/chat/ws?token=***" [accepted]',
        '1.2.3.4:5 - "GET /raw?access_token=*** HTTP/1.1" 200',
    ]
