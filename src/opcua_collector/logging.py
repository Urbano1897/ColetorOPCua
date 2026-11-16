"""JSON logs on stderr, with optional extra fields.

A collector spends its life unattended. One JSON object per line is easy to
ship to a log stack and still readable in `docker compose logs`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class JsonFormatter(logging.Formatter):
    """Format a log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """A compact single-line format used when JSON is in the way."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields:
            suffix = " ".join(f"{key}={value}" for key, value in fields.items())
            return f"{rendered} {suffix}"
        return rendered


class StructuredLogger:
    """Logger that accepts keyword fields without colliding with LogRecord."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, message: str, *, exc_info: bool = False, **fields: object) -> None:
        self._log(logging.DEBUG, message, fields, exc_info=exc_info)

    def info(self, message: str, **fields: object) -> None:
        self._log(logging.INFO, message, fields, exc_info=False)

    def warning(self, message: str, *, exc_info: bool = False, **fields: object) -> None:
        self._log(logging.WARNING, message, fields, exc_info=exc_info)

    def error(self, message: str, *, exc_info: bool = False, **fields: object) -> None:
        self._log(logging.ERROR, message, fields, exc_info=exc_info)

    def exception(self, message: str, **fields: object) -> None:
        self._logger.exception(message, extra={"fields": fields})

    def _log(
        self,
        level: int,
        message: str,
        fields: dict[str, object],
        *,
        exc_info: bool,
    ) -> None:
        self._logger.log(level, message, exc_info=exc_info, extra={"fields": fields})


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(logging.getLogger(name))


def configure_logging(level: str, log_format: str, stream: TextIO | None = None) -> None:
    """Configure the root logger. Safe to call once at process start."""
    numeric = _LEVELS.get(level.upper())
    if numeric is None:
        known = ", ".join(_LEVELS)
        raise SystemExit(f"error: unknown log level {level!r} (expected one of {known})")
    if log_format not in {"json", "text"}:
        raise SystemExit(f"error: unknown log format {log_format!r} (expected json or text)")

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric)

    # asyncua logs every publish at INFO, which drowns the collector.
    # DEBUG on our side still leaves the library at INFO.
    library_level = logging.INFO if numeric <= logging.DEBUG else logging.WARNING
    logging.getLogger("asyncua").setLevel(library_level)
