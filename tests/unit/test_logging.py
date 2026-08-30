import io
import json
import logging

from common.logging_setup import JsonLineFormatter, configure_logging


def test_configure_logging_emits_single_json_line():
    logger = configure_logging("test.logging.basic")
    stream = io.StringIO()
    logger.handlers[0].stream = stream

    logger.info("link degraded", extra={"link_id": "s2-s4", "probability": 0.84})

    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["message"] == "link degraded"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logging.basic"
    assert payload["link_id"] == "s2-s4"
    assert payload["probability"] == 0.84
    assert "timestamp" in payload


def test_configure_logging_does_not_duplicate_handlers():
    logger = configure_logging("test.logging.dedupe")
    configure_logging("test.logging.dedupe")
    configure_logging("test.logging.dedupe")
    assert len(logger.handlers) == 1


def test_formatter_handles_non_serializable_extra():
    formatter = JsonLineFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="event", args=(), exc_info=None,
    )
    record.weird = object()
    line = formatter.format(record)
    payload = json.loads(line)
    assert "weird" in payload
