"""Redact Uvicorn's ASGI error record without changing exception control flow."""

import logging
import re
from urllib.parse import unquote_plus

from app.core.error_handlers import _safe_error_locations


class _ASGIErrorFilter(logging.Filter):
    """Sanitize the known Uvicorn ASGI exception record before any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != "Exception in ASGI application\n":
            return True
        exc_info = record.exc_info
        # Redact first, so even a metadata extraction failure is fail-closed.
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        record.msg = "ASGI application failed"
        record.args = ()
        try:
            error = exc_info[1] if exc_info else None
            if error is not None:
                name = type(error).__name__
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
                    name = "Exception"
                record.msg = "ASGI application failed type=%s locations=%s"
                record.args = (name, _safe_error_locations(error))
        except Exception:
            # The safe static message is sufficient if metadata is malformed.
            pass
        return True


_SENSITIVE_QUERY_KEYS = frozenset({
    "token", "access_token", "refresh_token", "id_token", "api_key", "apikey",
    "password", "passwd", "secret", "client_secret", "authorization",
})


def _redact_query(path: str) -> str:
    """Preserve the request path and noncredential query fields for diagnostics."""
    prefix, separator, query = path.partition("?")
    if not separator:
        return path
    parts = []
    for part in query.split("&"):
        key, equals, _value = part.partition("=")
        if equals and unquote_plus(key).casefold() in _SENSITIVE_QUERY_KEYS:
            part = f"{key}=[REDACTED]"
        parts.append(part)
    return prefix + separator + "&".join(parts)


class _QueryCredentialFilter(logging.Filter):
    """Protect standard request records and WebSocket protocol debug details."""

    def filter(self, record: logging.LogRecord) -> bool:
        # websockets debug logs contain request headers and whole data frames, not
        # just the URI. Keep Uvicorn's own connection/status messages but omit
        # these low-level records, which can carry arbitrary user credentials.
        if (record.name == "uvicorn.error" and record.levelno <= logging.DEBUG
                and "websockets" in record.pathname.replace("\\", "/").split("/")):
            return False
        if record.name == "uvicorn.access" and record.msg == '%s - "%s %s HTTP/%s" %d':
            index = 2
        elif record.name == "uvicorn.error" and record.msg in (
            '%s - "WebSocket %s" [accepted]',
            '%s - "WebSocket %s" 403',
            '%s - "WebSocket %s" %d',
        ):
            index = 1
        else:
            return True
        if isinstance(record.args, tuple) and len(record.args) > index and isinstance(record.args[index], str):
            args = list(record.args)
            args[index] = _redact_query(args[index])
            record.args = tuple(args)
        return True


class _ASGITraceFilter(logging.Filter):
    """Uvicorn trace dumps ASGI scope and messages, including credentials."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno > 5


def install_uvicorn_asgi_error_filter() -> None:
    """Install once per process; Uvicorn uses this logger for ASGI failures."""
    server_logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(item, _ASGIErrorFilter) for item in server_logger.filters):
        server_logger.addFilter(_ASGIErrorFilter())
    for logger in (server_logger, logging.getLogger("uvicorn.access")):
        if not any(isinstance(item, _QueryCredentialFilter) for item in logger.filters):
            logger.addFilter(_QueryCredentialFilter())
    asgi_logger = logging.getLogger("uvicorn.asgi")
    if not any(isinstance(item, _ASGITraceFilter) for item in asgi_logger.filters):
        asgi_logger.addFilter(_ASGITraceFilter())
