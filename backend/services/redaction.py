"""
Secret redaction helpers for logs and API error text.

The Odds API key travels in the query string (``?apiKey=...``), so any
URL that httpx logs, or that appears inside an ``HTTPStatusError``
message, would otherwise leak the key into the console, snapshot
status, or HTTP error details.  ``redact_secrets`` masks the values of
known credential query parameters; ``SecretRedactionFilter`` applies it
to log records.
"""
from __future__ import annotations

import logging
import re

REDACTED = "REDACTED"

# Query/form parameters whose values are credentials.  Matches the
# parameter name (case-insensitive) after '?', '&', start, or whitespace,
# and masks everything up to the next delimiter.
_SECRET_PARAM_NAMES = (
    "apiKey",          # The Odds API (case-insensitive: also apikey/APIKEY)
    "api_key",
    "token",           # Pushover app token
    "access_token",
    "user",            # Pushover user key
)

_SECRET_PARAM_RE = re.compile(
    r"(?P<prefix>(?:^|[?&\s;,(\"'])(?:"
    + "|".join(re.escape(n) for n in _SECRET_PARAM_NAMES)
    + r")=)(?P<value>[^&\s'\";,)]+)",
    re.IGNORECASE,
)


def redact_secrets(text: object) -> str:
    """Return ``str(text)`` with credential query-param values masked."""
    s = text if isinstance(text, str) else str(text)
    if "=" not in s:
        return s
    return _SECRET_PARAM_RE.sub(lambda m: m.group("prefix") + REDACTED, s)


class SecretRedactionFilter(logging.Filter):
    """Logging filter that redacts credential values from a record's message.

    Never drops a record; it rewrites ``msg``/``args`` so the formatted
    message is redacted for every downstream handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


_REDACTED_LOGGERS = ("httpx", "httpcore")


def configure_log_redaction() -> None:
    """Attach SecretRedactionFilter to the httpx/httpcore loggers and to the
    root logger's handlers.  Idempotent.

    Logger-level filters only run for records created on that exact
    logger (not for children propagating up), so httpx/httpcore get
    their own filter; root *handler* filters cover records from every
    propagating logger (e.g. ``services.*`` error logs that format an
    exception containing the request URL).
    """
    for name in _REDACTED_LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(f, SecretRedactionFilter) for f in lg.filters):
            lg.addFilter(SecretRedactionFilter())
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(SecretRedactionFilter())
