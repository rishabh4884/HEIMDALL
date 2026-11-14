"""Logging configuration with secret redaction.

The redaction filter is a safety net. Code must still never log credentials.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable

from .config import Settings

LOGGER_NAME = "heimdall"
REDACTED = "***"


class RedactingFilter(logging.Filter):
    """Replaces any occurrence of known secret values in log messages."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, REDACTED)
        record.msg = message
        record.args = None
        if record.exc_info and record.exc_text is None:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            for secret in self._secrets:
                record.exc_text = record.exc_text.replace(secret, REDACTED)
        return True


def configure_logging(settings: Settings, stream=None) -> logging.Logger:
    """Configure the ``heimdall`` logger. Safe to call more than once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(settings.log_level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(settings.secret_values()))
    logger.addHandler(handler)
    return logger
