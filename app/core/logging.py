"""Logging with a redaction filter.

Log files travel further than any other piece of the system: they are tailed,
shipped to aggregators, and kept long past the retention window. Anything
resembling a personal identifier is scrubbed at record time — belt and braces
next to the transcript redactor, because a log leak is the most common way
sensitive data escapes a service.
"""

from __future__ import annotations

import logging
import re
import sys

_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\+?\d[\d \-().]{7,}\d"), "[phone]"),
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"), "[iban]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.I), "Bearer [redacted]"),
    (re.compile(r"(?i)token=[A-Za-z0-9._-]+"), "token=[redacted]"),
]


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        for pattern, replacement in _PATTERNS:
            message = pattern.sub(replacement, message)
        record.msg = message
        record.args = ()
        return True


_configured = False


def setup_logging(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    )
    handler.addFilter(RedactionFilter())
    root.handlers.clear()
    root.addHandler(handler)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
