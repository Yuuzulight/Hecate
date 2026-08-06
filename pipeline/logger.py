"""Structured logging.

One JSON object per line, so whatever is collecting logs out of the container
can parse them without a regex. Extra context goes in as keyword arguments:

    log.info("extracted", extra={"context": {"source": "github", "rows": 100}})
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(context)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str = "hecate") -> logging.Logger:
    """Return a logger writing JSON to stdout. Safe to call repeatedly."""
    global _configured
    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root = logging.getLogger("hecate")
        root.handlers = [handler]
        root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
        # - Don't hand records to the root logger as well, or every line doubles up.
        root.propagate = False
        _configured = True
    # - Has to be the "hecate" logger or a child of it, otherwise it misses the
    #   handler above and the line goes nowhere.
    if name == "hecate" or name.startswith("hecate."):
        return logging.getLogger(name)
    return logging.getLogger(f"hecate.{name}")
