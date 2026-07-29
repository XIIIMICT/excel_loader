import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.exc import InterfaceError, OperationalError

from app_logging import get_logger
from db import create_postgres_engine
from dictionary_models import SourceHistoryEntry
from excel_parser import read_workbook_metadata
from pd.db import PdLoadResult, load_report_to_db
from pd.excel_parser import parse_report
from pd.unsent import delete_unloaded_report, load_unloaded_reports, save_unloaded_report
from source_history import prepare_source_history
from source_resolver import SourceAuthError, SourceError, SourceFile, resolve_pattern_sources

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine

    from config import Settings
    from pd.models import ParsedReport

LOGGER = get_logger("pd")
DEFAULT_PD_FILE_PATTERN = r".*\.xlsx$"


@dataclass(frozen=True, slots=True)
class PdRunSummary:
    has_errors: bool
    has_format_changes: bool


@dataclass(frozen=True, slots=True)
class PdLoadAttempt:
    success: bool
    has_format_changes: bool


async def run_pd(settings: Settings) -> PdRunSummary:
    engine = create_postgres_engine(settings)
    try:
        history_table = await prepare_source_history(engine, users_to_grant=settings.pd_grant_select_users)
        unloaded_result = await _process_unloaded_reports(settings, engine, history_table=history_table)
        has_errors = not unloaded_result.success
        has_format_changes = unloaded_result.has_format_changes
        if has_errors:
            return PdRunSummary(has_errors=True, has_format_changes=has_format_changes)

        try:
            with resolve_pattern_sources(
                settings,
                source_config=settings.pd.sources,
                data_dir=settings.pd.sources.data_dir,
                default_file_pattern=DEFAULT_PD_FILE_PATTERN,
            ) as source_files:
                LOGGER.info("PD: найдено файлов по шаблону: %s.", len(source_files))
                for source_file in source_files:
                    attempt = await _process_source_file(settings, engine, source_file, history_table=history_table)
                    has_errors = has_errors or not attempt.success
                    has_format_changes = has_format_changes or attempt.has_format_changes
        except SourceAuthError as error:
            LOGGER.log(logging.ERROR, "PD: файлы не загружены. %s.", error)
            has_errors = True
        except SourceError:
            LOGGER.exception("PD: файлы не загружены из-за ошибки источника.")
            has_errors = True

        return PdRunSummary(has_errors=has_errors, has_format_changes=has_format_changes)
    finally:
        await engine.dispose()


async def _process_source_file(
    settings: Settings,
    engine: AsyncEngine,
    source_file: SourceFile,
    *,
    history_table: Table,
) -> PdLoadAttempt:
    loaded_at = datetime.now(tz=settings.app_timezone)
    try:
        LOGGER.info("PD: выбран источник %s.", source_file.display_name)
        report = parse_report(source_file.path, loaded_at=loaded_at)
        metadata = read_workbook_metadata(source_file.path, timezone=settings.app_timezone)
        report = replace(
            report,
            source_name=source_file.display_name,
            history_reference=source_file.history_reference,
            file_modified_at=metadata.file_modified_at,
            file_modifier=metadata.file_modifier,
        )
        _log_report_summary(report.source_name, report)
        return await _load_or_save_report(settings, engine, report, history_table=history_table)
    except FileNotFoundError:
        LOGGER.exception("PD: файл не загружен, потому что не найден.")
    except ValueError:
        LOGGER.exception("PD: файл не загружен из-за ошибки разбора.")
    except Exception:
        LOGGER.exception("PD: файл не загружен. Внутренняя ошибка обработки.")

    return PdLoadAttempt(success=False, has_format_changes=False)


async def _process_unloaded_reports(
    settings: Settings,
    engine: AsyncEngine,
    *,
    history_table: Table,
) -> PdLoadAttempt:
    unloaded_reports = load_unloaded_reports(settings.unloaded_data_dir)
    if not unloaded_reports:
        return PdLoadAttempt(success=True, has_format_changes=False)

    LOGGER.info("PD: найдены отложенные пакеты: %s.", len(unloaded_reports))
    has_format_changes = False
    for package_dir, report in unloaded_reports:
        try:
            _log_report_summary(f"unloaded:{package_dir.name}", report)
            result = await load_report_to_db(
                settings,
                engine,
                report,
                history_table=history_table,
                history_entry=_source_history_entry(settings, report),
            )
        except Exception as error:
            if _is_database_unavailable(error):
                LOGGER.exception(
                    "PD: база данных недоступна, пакет оставлен в unloaded_data: %s.",
                    package_dir,
                )
                return PdLoadAttempt(success=False, has_format_changes=has_format_changes)
            LOGGER.exception("PD: отложенный пакет %s не загружен из-за внутренней ошибки.", package_dir)
            return PdLoadAttempt(success=False, has_format_changes=has_format_changes)

        delete_unloaded_report(package_dir)
        _log_format_changes(result)
        has_format_changes = has_format_changes or result.has_format_changes
        LOGGER.info(
            "PD: пакет %s загружен. Дата=%s, удалено=%s, вставлено=%s.",
            package_dir.name,
            report.report_date.isoformat(),
            result.deleted_rows,
            result.inserted_rows,
        )

    return PdLoadAttempt(success=True, has_format_changes=has_format_changes)


async def _load_or_save_report(
    settings: Settings,
    engine: AsyncEngine,
    report: ParsedReport,
    *,
    history_table: Table,
) -> PdLoadAttempt:
    try:
        result = await load_report_to_db(
            settings,
            engine,
            report,
            history_table=history_table,
            history_entry=_source_history_entry(settings, report),
        )
    except Exception as error:
        if not _is_database_unavailable(error):
            raise

        package_dir = save_unloaded_report(settings.unloaded_data_dir, report)
        LOGGER.exception(
            "PD: база данных недоступна, отчет сохранен в unloaded_data: %s.",
            package_dir,
        )
        return PdLoadAttempt(success=False, has_format_changes=False)

    _log_format_changes(result)
    LOGGER.info(
        "PD: загрузка завершена. Дата=%s, удалено=%s, вставлено=%s.",
        report.report_date.isoformat(),
        result.deleted_rows,
        result.inserted_rows,
    )
    return PdLoadAttempt(success=True, has_format_changes=result.has_format_changes)


def _source_history_entry(settings: Settings, report: ParsedReport) -> SourceHistoryEntry | None:
    if report.history_reference is None or report.file_modified_at is None or report.file_modifier is None:
        LOGGER.warning("PD: для старого отложенного пакета недоступны метаданные истории источника.")
        return None
    return SourceHistoryEntry(
        loaded_at=report.loaded_at,
        src_file=report.history_reference,
        dest_table=settings.pd_table_schema_reference("pd_data"),
        file_modified_at=report.file_modified_at,
        file_modifier=report.file_modifier,
    )


def _log_report_summary(source_name: str, report: ParsedReport) -> None:
    LOGGER.info(
        "PD: распознано из %s: дата=%s, активов=%s, строк сырья/номенклатуры=%s, показателей=%s, строк данных=%s.",
        source_name,
        report.report_date.isoformat(),
        len(report.assets),
        len(report.raw_materials),
        len(report.indicators),
        len(report.data),
    )


def _log_format_changes(result: PdLoadResult) -> None:
    for change in result.changes:
        LOGGER.error(
            "PD: изменение формата/справочника: action=%s, entity_type=%s, entity_name=%s, details=%s.",
            change.action,
            change.entity_type,
            change.entity_name,
            change.details or "",
        )


def _is_database_unavailable(error: BaseException) -> bool:
    if isinstance(error, (ConnectionError, OSError, InterfaceError, OperationalError)):
        return True
    return any(_is_database_unavailable(item) for item in (error.__cause__, error.__context__) if item is not None)
