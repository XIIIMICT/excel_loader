import json
import logging
import sys
import time
from contextvars import ContextVar, Token
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings

LOGGER_ROOT = "excel_loader"
# Каждый запуск создаёт новый файл logs/<timestamp>.log, поэтому каталог
# растёт неограниченно, пока не случится такая чистка. 14 дней хватает,
# чтобы сопоставить прошлые загрузки или разобрать старый инцидент.
LOG_RETENTION_DAYS = 14
LOG_FILE_SUFFIX = ".log"
CURRENT_MODULE: ContextVar[str | None] = ContextVar("excel_loader_module", default=None)
ANSI_RESET = "\x1b[0m"
LEVEL_COLORS = {
    logging.DEBUG: "\x1b[36m",  # cyan
    logging.INFO: "\x1b[32m",  # green
    logging.WARNING: "\x1b[33m",  # yellow
    logging.ERROR: "\x1b[31m",  # red
    logging.CRITICAL: "\x1b[1;31m",  # bold red
}


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
            "module": CURRENT_MODULE.get() or _module_from_logger(record.name),
            "message": record.getMessage(),
            "run_id": self.run_id,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """
    Human-readable line for the terminal; the file keeps the JSON schema.

    One logger, two handlers: this formatter serves ``StreamHandler`` (the
    person watching the run), ``JsonFormatter`` serves the per-run file that
    Alloy ships to Loki. Color is opt-in and enabled only for a TTY, so
    redirected output, CI logs and ``docker compose logs`` stay free of escape
    sequences.
    """

    def __init__(self, *, timezone: tzinfo, use_color: bool = False) -> None:
        super().__init__()
        self.timezone = timezone
        self.use_color = use_color

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: ARG002, N802
        timestamp = datetime.fromtimestamp(record.created, tz=self.timezone)
        return timestamp.strftime("%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        module = CURRENT_MODULE.get() or _module_from_logger(record.name)
        level = f"{record.levelname:<8}"
        color = LEVEL_COLORS.get(record.levelno) if self.use_color else None
        if color is not None:
            level = f"{color}{level}{ANSI_RESET}"
        line = f"{self.formatTime(record)} {level} {module}: {record.getMessage()}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            line = f"{line}\n{self.formatStack(record.stack_info)}"
        return line


def configure_logging(settings: Settings, started_at: datetime) -> Path:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.logs_dir / f"{started_at.strftime('%d.%m.%Y-%H.%M.%S')}{LOG_FILE_SUFFIX}"

    json_formatter = JsonFormatter(
        timezone=settings.app_timezone,
        run_id=started_at.isoformat(timespec="seconds"),
    )
    console_formatter = ConsoleFormatter(
        timezone=settings.app_timezone,
        use_color=sys.stderr.isatty(),
    )

    logger = logging.getLogger(LOGGER_ROOT)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    # The file is the machine-readable stream: Alloy tails it into Loki, so the
    # JSON schema (timestamp/level/logger/module/message/run_id) stays stable.
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(json_formatter)

    # The console is for the person watching the run; it is not parsed.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(console_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    _prune_old_logs(settings.logs_dir)

    return log_path


def get_logger(name: str) -> logging.Logger:
    suffix = name.strip(".")
    logger_name = LOGGER_ROOT if not suffix else f"{LOGGER_ROOT}.{suffix}"
    return logging.getLogger(logger_name)


def set_log_module(module: str) -> Token[str | None]:
    return CURRENT_MODULE.set(module)


def reset_log_module(token: Token[str | None]) -> None:
    CURRENT_MODULE.reset(token)


def _module_from_logger(logger_name: str) -> str:
    if logger_name == LOGGER_ROOT:
        return "common"
    return logger_name.removeprefix(f"{LOGGER_ROOT}.").split(".", maxsplit=1)[0]


def _prune_old_logs(logs_dir: Path) -> None:
    cutoff = time.time() - LOG_RETENTION_DAYS * 24 * 60 * 60
    removed: list[str] = []
    for path in sorted(logs_dir.glob(f"*{LOG_FILE_SUFFIX}")):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path.name)
        except OSError:
            # Файл может быть занят другим процессом — оставим до следующего запуска.
            continue

    _logger().info("Удалены лог-файлы старше %d дней: %s.", LOG_RETENTION_DAYS, ", ".join(removed) or "нет")


def _logger() -> logging.Logger:
    return logging.getLogger(LOGGER_ROOT)
