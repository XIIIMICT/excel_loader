"""
Logging unit tests for ``app_logging``.

Covers ``JsonFormatter`` fields, the module fallback, ``get_logger`` naming and
``configure_logging`` (JSON-lines file + retention pruning of old logs).
"""

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import app_logging
from config import Settings

if TYPE_CHECKING:
    from pathlib import Path

MODULE_CODES = ("quotes_nsi", "quotes", "pd", "sales_op")


def _settings(logs_dir: Path) -> Settings:
    module = {"schema": "raw", "sources": {"data_dir": "data"}}
    data: dict[str, object] = dict.fromkeys(MODULE_CODES, module)
    data["common"] = {"log_dir": logs_dir}
    return Settings.model_validate(data)


def _record(name: str, message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=None,
        exc_info=None,
    )


def _formatted(formatter: app_logging.JsonFormatter, record: logging.LogRecord) -> dict[str, object]:
    return json.loads(formatter.format(record))


def test_json_formatter_emits_stable_fields() -> None:
    formatter = app_logging.JsonFormatter(timezone=ZoneInfo("UTC"), run_id="run-1")
    record = _record("excel_loader.main", "старт загрузки")

    payload = _formatted(formatter, record)

    assert payload["level"] == "INFO"
    assert payload["logger"] == "excel_loader.main"
    assert payload["module"] == "main"
    assert payload["message"] == "старт загрузки"
    assert payload["run_id"] == "run-1"
    assert "exception" not in payload
    assert "stack" not in payload


def test_json_formatter_timestamp_applies_the_configured_timezone() -> None:
    formatter = app_logging.JsonFormatter(timezone=ZoneInfo("Europe/Moscow"), run_id="run-1")

    timestamp = str(_formatted(formatter, _record("excel_loader", "x"))["timestamp"])

    assert timestamp.endswith("+03:00")  # Moscow is always UTC+3; UTC would render +00:00


def test_json_formatter_module_falls_back_to_the_logger_name() -> None:
    formatter = app_logging.JsonFormatter(timezone=UTC, run_id="run-1")

    root_payload = _formatted(formatter, _record("excel_loader", "x"))
    nested_payload = _formatted(formatter, _record("excel_loader.pd.runner", "x"))

    assert root_payload["module"] == "common"
    assert nested_payload["module"] == "pd"


def test_json_formatter_contextvar_module_wins() -> None:
    formatter = app_logging.JsonFormatter(timezone=UTC, run_id="run-1")
    token = app_logging.set_log_module("quotes")
    try:
        payload = _formatted(formatter, _record("excel_loader.quotes.__init__", "x"))
    finally:
        app_logging.reset_log_module(token)

    assert payload["module"] == "quotes"


def test_json_formatter_adds_exception_field() -> None:
    formatter = app_logging.JsonFormatter(timezone=UTC, run_id="run-1")
    try:
        error = ValueError("падение")
        raise error
    except ValueError:
        record = _record("excel_loader", "сломалось")
        record.exc_info = (ValueError, error, error.__traceback__)

    payload = _formatted(formatter, record)

    assert "ValueError" in str(payload["exception"])
    assert "падение" in str(payload["exception"])


def test_get_logger_naming() -> None:
    assert app_logging.get_logger("").name == "excel_loader"
    assert app_logging.get_logger("pd").name == "excel_loader.pd"
    assert app_logging.get_logger("pd.").name == "excel_loader.pd"


def test_configure_logging_writes_json_lines_and_prunes_old_logs(tmp_path: Path) -> None:
    outdated = tmp_path / "01.01.2000-00.00.00.log"
    outdated.write_text("old\n", encoding="utf-8")
    stale_time = (datetime.now(UTC) - timedelta(days=app_logging.LOG_RETENTION_DAYS + 2)).timestamp()
    os.utime(outdated, (stale_time, stale_time))
    recent_holder = tmp_path / "recent.log"
    recent_holder.write_text("recent\n", encoding="utf-8")

    log_path = app_logging.configure_logging(_settings(tmp_path), started_at=datetime.now(UTC))

    assert log_path.exists()
    assert log_path.name.endswith(app_logging.LOG_FILE_SUFFIX)
    assert not outdated.exists()
    assert recent_holder.exists()
    root = logging.getLogger(app_logging.LOGGER_ROOT)
    try:
        root.info("проверка записи %s", "json")
    finally:
        for handler in list(root.handlers):
            handler.close()
        root.handlers.clear()

    json_lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    assert json_lines[-1]["message"] == "проверка записи json"
    assert json_lines[-1]["module"] == "common"
    assert json_lines[-1]["run_id"] != ""
    assert json_lines[-1]["timestamp"] != ""
