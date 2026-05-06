import json
import logging
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings

LOGGER_ROOT = "excel_loader"


class JsonFormatter(logging.Formatter):
    def __init__(self, *, timezone: tzinfo, run_id: str) -> None:
        super().__init__()
        self.timezone = timezone
        self.run_id = run_id

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: ARG002, N802
        timestamp = datetime.fromtimestamp(record.created, tz=self.timezone)
        return timestamp.isoformat(timespec="milliseconds")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "module": _module_from_logger(record.name),
            "message": record.getMessage(),
            "run_id": self.run_id,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(settings: Settings, started_at: datetime) -> Path:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.logs_dir / f"{started_at.strftime('%d.%m.%Y-%H.%M.%S')}.log"

    formatter = JsonFormatter(
        timezone=settings.app_timezone,
        run_id=started_at.isoformat(timespec="seconds"),
    )

    logger = logging.getLogger(LOGGER_ROOT)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return log_path


def get_logger(name: str) -> logging.Logger:
    suffix = name.strip(".")
    logger_name = LOGGER_ROOT if not suffix else f"{LOGGER_ROOT}.{suffix}"
    return logging.getLogger(logger_name)


def flush_log_handlers() -> None:
    logger = logging.getLogger(LOGGER_ROOT)
    for handler in logger.handlers:
        handler.flush()


def _module_from_logger(logger_name: str) -> str:
    if logger_name == LOGGER_ROOT:
        return "common"
    return logger_name.removeprefix(f"{LOGGER_ROOT}.").split(".", maxsplit=1)[0]
