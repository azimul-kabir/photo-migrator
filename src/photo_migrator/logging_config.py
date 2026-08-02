"""Consistent text and JSON logging for every command."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    """Render stable, one-object-per-line diagnostic logs."""

    fields = ("command", "run_id", "asset_id", "source_path", "destination_path")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in self.fields:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            exception_type = record.exc_info[0]
            payload["error_type"] = exception_type.__name__ if exception_type else "Exception"
            if record.levelno <= logging.DEBUG:
                payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def configure_logging(level: str, log_format: str, log_file: Path | None) -> None:
    """Replace root handlers with the requested safe logging configuration."""
    handler: logging.Handler = (
        logging.FileHandler(log_file, encoding="utf-8") if log_file else logging.StreamHandler()
    )
    handler.setFormatter(
        JsonFormatter()
        if log_format == "json"
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logging.basicConfig(level=getattr(logging, level), handlers=[handler], force=True)
