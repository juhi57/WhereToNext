"""Structured logging.

The interesting case here is a real bug this project hit: passing
``extra={"message": ...}`` makes ``logging.makeRecord`` raise ``KeyError``,
because ``message`` is one of ``LogRecord``'s own attributes. It surfaced on the
AI-failure fallback path - the one place that exists specifically to keep the
app alive - so the reserved-name rule is worth a test of its own.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.logging import _STANDARD, JsonFormatter, configure_logging, get_logger


@pytest.fixture
def formatter() -> JsonFormatter:
    return JsonFormatter()


def record(**extra: object) -> logging.LogRecord:
    logger = logging.getLogger("test.logger")
    return logger.makeRecord(
        "test.logger", logging.INFO, __file__, 1, "event_name", (), None, extra=extra or None
    )


class TestJsonFormatter:
    def test_emits_one_json_object_per_record(self, formatter: JsonFormatter) -> None:
        payload = json.loads(formatter.format(record()))
        assert payload["level"] == "INFO"
        assert payload["logger"] == "test.logger"
        assert payload["message"] == "event_name"
        assert payload["ts"].endswith("Z")

    def test_extra_fields_are_merged_in(self, formatter: JsonFormatter) -> None:
        payload = json.loads(formatter.format(record(itinerary_id="abc", duration_ms=12)))
        assert payload["itinerary_id"] == "abc"
        assert payload["duration_ms"] == 12

    def test_output_is_ascii_so_windows_consoles_survive(self, formatter: JsonFormatter) -> None:
        """cp1252 consoles cannot encode a rupee sign; escaping keeps logs safe."""
        line = formatter.format(record(note="cost ₹1,000"))
        line.encode("ascii")  # must not raise
        assert "\\u20b9" in line

    def test_non_serialisable_values_do_not_break_the_line(self, formatter: JsonFormatter) -> None:
        payload = json.loads(formatter.format(record(obj=object())))
        assert isinstance(payload["obj"], str)

    def test_exceptions_are_included(self, formatter: JsonFormatter) -> None:
        logger = logging.getLogger("test.logger")
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            rec = logger.makeRecord(
                "test.logger", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        payload = json.loads(formatter.format(rec))
        assert "ValueError: boom" in payload["exception"]


class TestReservedNames:
    @pytest.mark.parametrize("name", ["message", "asctime", "args", "levelname", "module"])
    def test_reserved_keys_are_rejected_by_logging(self, name: str) -> None:
        """Documents why the codebase must not use these as ``extra`` keys."""
        with pytest.raises(KeyError):
            record(**{name: "x"})

    def test_the_formatter_knows_the_reserved_set(self) -> None:
        assert {"message", "asctime", "levelname", "args"} <= _STANDARD


class TestConfiguration:
    def test_configure_logging_installs_a_single_json_handler(self) -> None:
        configure_logging("INFO")
        root = logging.getLogger()
        try:
            assert len(root.handlers) == 1
            assert isinstance(root.handlers[0].formatter, JsonFormatter)
            assert logging.getLogger("httpx").level == logging.WARNING
        finally:
            root.handlers.clear()

    def test_get_logger_returns_a_namespaced_logger(self) -> None:
        assert get_logger("app.thing").name == "app.thing"
