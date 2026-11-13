from collections import defaultdict
from typing import TYPE_CHECKING

from app_logging import get_logger
from business_errors import BusinessLoadError
from db import postgres_session
from dictionary_models import SourceHistoryEntry
from module_models import ModuleOutcome
from postgres import inspect_storage, replace_table_rows
from sales_op.db import create_sales_op_view
from sales_op.excel_parser import inspect_sales_op_workbook, parse_sales_op_workbook
from sales_op.models import (
    SalesOpError,
    SelectedSalesOpFile,
)
from sales_op.specification import SPECIFICATION_FILE_NAME, read_sales_op_specification, source_table_name
from schema_definition import build_dictionary_table
from source_history import prepare_source_history
from source_resolver import SourceError, resolve_all_pattern_sources
from storage import prepare_storage

if TYPE_CHECKING:
    from datetime import tzinfo

    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine

    from config import Settings
    from dictionary_models import DbRow
    from sales_op.models import SalesOpSourceSpecification, SalesOpSpecification
    from source_resolver import SourceFile

LOGGER = get_logger("sales_op")
DEFAULT_FILE_PATTERN = r".*\.xlsx$"


async def run_sales_op(settings: Settings) -> ModuleOutcome:
    module = settings.modules["sales_op"]
    data_dir = module.sources.data_dir
    try:
        specification = read_sales_op_specification(data_dir / SPECIFICATION_FILE_NAME)
    except SalesOpError as error:
        LOGGER.log(40, "План продаж не загружен. Причина: %s.", error)
        return _sales_op_outcome(has_errors=True, loaded_tables=(), view_reference=None, has_warnings=False)

    try:
        with resolve_all_pattern_sources(
            settings,
            source_config=module.sources,
            data_dir=data_dir,
            default_file_pattern=DEFAULT_FILE_PATTERN,
            recursive_local=True,
            skip_processed_sales_op_files=True,
        ) as source_files:
            selected_files, selection_warnings, inspection_failures = _select_source_files(
                source_files,
                specification_sources=specification.sources,
                timezone=settings.app_timezone,
            )
            rows_by_source, history_by_source, parse_warnings, parse_failures, parsed_files = _parse_selected_files(
                selected_files,
                specification_sources=specification.sources,
            )
    except SourceError as error:
        LOGGER.log(40, "План продаж не загружен. Причина: %s.", error)
        return _sales_op_outcome(has_errors=True, loaded_tables=(), view_reference=None, has_warnings=False)

    async with postgres_session(settings) as engine:
        loaded_tables, storage_failures = await _replace_tables(
            settings,
            engine=engine,
            specification=specification,
            rows_by_source=rows_by_source,
            history_by_source=history_by_source,
        )
        loaded_source_codes = {table.name.removeprefix("sales_op_") for table in loaded_tables}
        rename_warnings = _mark_processed_files(parsed_files, loaded_source_codes=loaded_source_codes)
        view_reference = None
        view_failures = 0
        try:
            view_reference = await create_sales_op_view(
                settings,
                engine=engine,
                specification=specification,
            )
        except Exception:
            view_failures = 1
            LOGGER.exception("Не удалось создать представление sales_op_raw_view.")

    return _sales_op_outcome(
        has_errors=inspection_failures + parse_failures + storage_failures + view_failures > 0,
        loaded_tables=loaded_tables,
        view_reference=view_reference,
        has_warnings=selection_warnings + parse_warnings + rename_warnings > 0,
    )


def _sales_op_outcome(
    *,
    has_errors: bool,
    loaded_tables: tuple[Table, ...],
    view_reference: str | None,
    has_warnings: bool,
) -> ModuleOutcome:
    return ModuleOutcome(
        has_errors=has_errors,
        has_warnings=has_warnings,
        has_format_changes=False,
        tables_to_grant=loaded_tables,
        relation_references_to_grant=(view_reference,) if view_reference else (),
    )


def _select_source_files(
    source_files: tuple[SourceFile, ...],
    *,
    specification_sources: dict[str, SalesOpSourceSpecification],
    timezone: tzinfo,
) -> tuple[tuple[SelectedSalesOpFile, ...], int, int]:
    candidates: dict[str, list[SelectedSalesOpFile]] = defaultdict(list)
    failures = 0
    for source_file in source_files:
        original_name = source_file.original_name or source_file.path.name
        source_code = _classify_file(original_name, specification_sources=specification_sources)
        if source_code is None:
            continue
        try:
            plan_date, modified_at, metadata = inspect_sales_op_workbook(source_file.path, timezone=timezone)
        except Exception:
            failures += 1
            LOGGER.exception("План продаж: не удалось определить дату файла %s.", source_file.display_name)
            continue
        candidates[source_code].append(
            SelectedSalesOpFile(
                source_code=source_code,
                path=source_file.path,
                display_name=source_file.display_name,
                original_name=original_name,
                plan_date=plan_date,
                modified_at=modified_at,
                metadata=metadata,
                history_reference=source_file.history_reference,
            ),
        )

    selected: list[SelectedSalesOpFile] = []
    warnings = 0
    for source_code, source_specification in specification_sources.items():
        source_candidates = candidates[source_code]
        if not source_candidates:
            warnings += 1
            LOGGER.warning(
                "План продаж %s: файлы по шаблону %r не найдены.",
                source_code,
                source_specification.file_pattern,
            )
            continue
        by_date: dict[object, list[SelectedSalesOpFile]] = defaultdict(list)
        for candidate in source_candidates:
            by_date[candidate.plan_date].append(candidate)
        for plan_date, date_candidates in sorted(by_date.items(), key=lambda item: item[0]):
            winner = max(date_candidates, key=lambda item: (item.modified_at, item.original_name.casefold()))
            selected.append(winner)
            if len(date_candidates) > 1:
                LOGGER.warning(
                    "План продаж %s: для даты %s найдено несколько файлов, выбран самый поздний: %s.",
                    source_code,
                    plan_date,
                    winner.display_name,
                )
                warnings += 1
    return tuple(selected), warnings, failures


def _parse_selected_files(
    selected_files: tuple[SelectedSalesOpFile, ...],
    *,
    specification_sources: dict[str, SalesOpSourceSpecification],
) -> tuple[
    dict[str, list[DbRow]],
    dict[str, list[SourceHistoryEntry]],
    int,
    int,
    tuple[SelectedSalesOpFile, ...],
]:
    rows_by_source: dict[str, list[DbRow]] = defaultdict(list)
    history_by_source: dict[str, list[SourceHistoryEntry]] = defaultdict(list)
    parsed_files: list[SelectedSalesOpFile] = []
    warnings = 0
    failures = 0
    for selected_file in selected_files:
        try:
            parsed = parse_sales_op_workbook(
                selected_file.path,
                source_specification=specification_sources[selected_file.source_code],
                metadata=selected_file.metadata,
            )
        except Exception:
            failures += 1
            LOGGER.exception("План продаж: файл %s не обработан.", selected_file.display_name)
            continue
        parsed_files.append(selected_file)
        rows_by_source[selected_file.source_code].extend(parsed.rows)
        source_specification = specification_sources[selected_file.source_code]
        metadata = selected_file.metadata
        history_by_source[selected_file.source_code].append(
            SourceHistoryEntry(
                loaded_at=metadata.loaded_at,
                src_file=selected_file.history_reference,
                dest_table=source_table_name(source_specification.code),
                file_modified_at=metadata.file_modified_at,
                file_modifier=metadata.file_modifier,
            ),
        )
        warnings += parsed.warning_count
        LOGGER.info(
            "План продаж %s: файл %s распознан: лист=%s, дата=%s, строк=%s.",
            selected_file.source_code,
            selected_file.display_name,
            parsed.sheet_name,
            parsed.plan_date,
            len(parsed.rows),
        )
    return dict(rows_by_source), dict(history_by_source), warnings, failures, tuple(parsed_files)


async def _replace_tables(
    settings: Settings,
    *,
    engine: AsyncEngine,
    specification: SalesOpSpecification,
    rows_by_source: dict[str, list[DbRow]],
    history_by_source: dict[str, list[SourceHistoryEntry]],
) -> tuple[tuple[Table, ...], int]:
    loaded_tables: list[Table] = []
    failures = 0
    history_table = await prepare_source_history(engine, users_to_grant=settings.modules["sales_op"].users_to_grant)
    for source_code, source_specification in specification.sources.items():
        if source_code not in rows_by_source:
            continue
        table = build_dictionary_table(
            table_reference=settings.modules["sales_op"].table_reference(source_table_name(source_code)),
            columns=source_specification.table_columns,
            primary_key_columns=(),
        )
        try:
            storage = await inspect_storage(table, engine=engine)
            if storage.needs_seed:
                await prepare_storage(table, engine=engine, users_to_grant=settings.modules["sales_op"].users_to_grant)
            entries = tuple(
                SourceHistoryEntry(
                    loaded_at=entry.loaded_at,
                    src_file=entry.src_file,
                    dest_table=f"{table.schema}.{table.name}",
                    file_modified_at=entry.file_modified_at,
                    file_modifier=entry.file_modifier,
                )
                for entry in history_by_source.get(source_code, ())
            )
            inserted = await replace_table_rows(
                engine,
                table,
                rows=rows_by_source[source_code],
                history_table=history_table,
                history_entries=entries,
            )
        except BusinessLoadError as error:
            failures += 1
            LOGGER.log(
                40,
                "Не удалось заменить данные таблицы %s.%s. Причина: %s.",
                table.schema,
                table.name,
                error.reason,
            )
            continue
        except Exception:
            failures += 1
            LOGGER.exception("Не удалось заменить данные таблицы %s.%s.", table.schema, table.name)
            continue
        loaded_tables.append(table)
        LOGGER.info("Данные таблицы %s.%s заменены: строк=%s.", table.schema, table.name, inserted)
    return tuple(loaded_tables), failures


def _mark_processed_files(
    parsed_files: tuple[SelectedSalesOpFile, ...],
    *,
    loaded_source_codes: set[str],
) -> int:
    """
    Rename files whose plan was parsed and stored without errors.

    A leading underscore marks the file as already loaded: the next run skips
    it (see ``_is_processed_sales_op_file``). A file whose table failed to
    replace is left untouched so it is retried on the next run; a rename that
    fails is only a warning, never a load error.
    """
    warnings = 0
    for selected_file in parsed_files:
        if selected_file.source_code not in loaded_source_codes:
            continue
        if selected_file.display_name.startswith("IMAP:"):
            # IMAP attachments live in a temporary folder; renaming them has
            # no effect on the next run's discovery.
            continue
        source_path = selected_file.path
        renamed_path = source_path.with_name(f"_{source_path.name}")
        try:
            source_path.rename(renamed_path)
        except OSError as error:
            warnings += 1
            LOGGER.warning(
                "План продаж: не удалось переименовать обработанный файл %s в %s: %s.",
                source_path,
                renamed_path.name,
                error,
            )
            continue
        LOGGER.info("План продаж: обработанный файл %s переименован в %s.", source_path.name, renamed_path.name)
    return warnings


def _classify_file(file_name: str, *, specification_sources: dict[str, SalesOpSourceSpecification]) -> str | None:
    for source_code, source_specification in specification_sources.items():
        if source_specification.matches_file_name(file_name):
            return source_code
    return None
