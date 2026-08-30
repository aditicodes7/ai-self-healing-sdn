"""stdlib logging configured to emit one JSON object per line.

Every service in this project (controller, ml_rl_service, backend,
experiment runners) calls configure_logging() once at startup so that logs
across the two venvs and the SQLite-backed event_logger are uniformly
parseable, and so extra structured fields (link_id, incident_id, decision
reason, ...) survive as real JSON keys instead of being interpolated into a
free-text message.
"""
from __future__ import annotations

import json
import logging
import sys
import time

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys())


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": round(record.created, 6),
            "iso_time": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(record.created)
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key in payload:
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = repr(value)
            payload[key] = value

        return json.dumps(payload, default=str)


def configure_logging(name: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, JsonLineFormatter)
               for h in logger.handlers):
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(JsonLineFormatter())
        logger.addHandler(handler)

    return logger
