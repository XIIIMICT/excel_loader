from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app_logging import get_logger
from db import create_postgres_engine
from postgres import inspect_storage, replace_table_rows
from sales_op.db import create_sales_op_view
from sales_op.excel_parser import inspect_sales_op_workbook, parse_sales_op_workbook
from sales_op.models import (
    SalesOpError,
    SelectedSalesOpFile,
)
from sales_op.registry import SOURCE_DEFINITIONS
from sales_op.specification import SPECIFICATION_FILE_NAME, read_sales_op_specification
from schema_definition import build_dictionary_table
from source_resolver import SourceError, resolve_all_pattern_sources
from storage import prepare_storage

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine

    from config import Settings
    from dictionary_models import DbRow
    from sales_op.models import SalesOpSourceSpecification, SalesOpSpecification
    from source_resolver import SourceFile

LOGGER = get_logger("sales_op")
DEFAULT_FILE_PATTERN = r".*\.xlsx$"


@dataclass(frozen=True, slots=True)
class SalesOpRunSummary:
    failed_items: int
    warning_items: int
    loaded_tables: tuple[Table, ...]
    view_reference: str | None

    @property
    def has_errors(self) -> bool:
        return self.failed_items > 0

    @property
    def has_warnings(self) -> bool:
        return self.warning_items > 0


async def run_sales_op(settings: Settings) -> SalesOpRunSummary:
    data_dir = settings.sales_op.sources.data_dir
    try:
        specification = read_sales_op_specification(data_dir / SPECIFICATION_FILE_NAME)
    except SalesOpError as error:
        LOGGER.log(40, "План продаж не загружен. Причина: %s.", error)
        return SalesOpRunSummary(1, 0, (), None)

    try:
        with resolve_all_pattern_sources(
            settings,
            source_config=settings.sales_op.sources,
            data_dir=data_dir,
            default_file_pattern=DEFAULT_FILE_PATTERN,
            recursive_local=True,
        ) as source_files:
            selected_files, selection_warnings, inspection_failures = _select_source_files(source_files)
            rows_by_source, parse_warnings, parse_failures = _parse_selected_files(
                selected_files,
                specification_sources=specification.sources,
            )
    except SourceError as error:
        LOGGER.log(40, "План продаж не загружен. Причина: %s.", error)
        return SalesOpRunSummary(1, 0, (), None)

    engine = create_postgres_engine(settings)
    try:
        loaded_tables, storage_failures = await _replace_tables(
            settings,
            engine=engine,
            specification=specification,
            rows_by_source=rows_by_source,
        )
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
    finally:
        await engine.dispose()

    return SalesOpRunSummary(
        failed_items=inspection_failures + parse_failures + storage_failures + view_failures,
        warning_items=selection_warnings + parse_warnings,
        loaded_tables=loaded_tables,
        view_reference=view_reference,
    )


def _select_source_files(
    source_files: tuple[SourceFile, ...],
) -> tuple[tuple[SelectedSalesOpFile, ...], int, int]:
    candidates: dict[str, list[SelectedSalesOpFile]] = defaultdict(list)
    failures = 0
    for source_file in source_files:
        original_name = source_file.original_name or source_file.path.name
        source_code = _classify_file(original_name)
        if source_code is None:
            continue
        try:
            plan_date, modified_at = inspect_sales_op_workbook(source_file.path)
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
            ),
        )

    selected: list[SelectedSalesOpFile] = []
    warnings = 0
    for definition in SOURCE_DEFINITIONS:
        source_candidates = candidates[definition.code]
        if not source_candidates:
            warnings += 1
            LOGGER.warning("План продаж %s: файлы по шаблону %r не найдены.", definition.code, definition.file_pattern)
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
                    definition.code,
                    plan_date,
                    winner.display_name,
                )
                warnings += 1
    return tuple(selected), warnings, failures


def _parse_selected_files(
    selected_files: tuple[SelectedSalesOpFile, ...],
    *,
    specification_sources: dict[str, SalesOpSourceSpecification],
) -> tuple[dict[str, list[DbRow]], int, int]:
    rows_by_source: dict[str, list[DbRow]] = defaultdict(list)
    warnings = 0
    failures = 0
    for selected_file in selected_files:
        try:
            parsed = parse_sales_op_workbook(
                selected_file.path,
                source_specification=specification_sources[selected_file.source_code],
            )
        except Exception:
            failures += 1
            LOGGER.exception("План продаж: файл %s не обработан.", selected_file.display_name)
            continue
        rows_by_source[selected_file.source_code].extend(parsed.rows)
        warnings += parsed.warning_count
        LOGGER.info(
            "План продаж %s: файл %s распознан: лист=%s, дата=%s, строк=%s.",
            selected_file.source_code,
            selected_file.display_name,
            parsed.sheet_name,
            parsed.plan_date,
            len(parsed.rows),
        )
    return dict(rows_by_source), warnings, failures


async def _replace_tables(
    settings: Settings,
    *,
    engine: AsyncEngine,
    specification: SalesOpSpecification,
    rows_by_source: dict[str, list[DbRow]],
) -> tuple[tuple[Table, ...], int]:
    loaded_tables: list[Table] = []
    failures = 0
    for definition in SOURCE_DEFINITIONS:
        if definition.code not in rows_by_source:
            continue
        table = build_dictionary_table(
            table_reference=settings.sales_op_table_schema_reference(definition.table_name),
            columns=specification.sources[definition.code].table_columns,
            primary_key_columns=(),
        )
        try:
            storage = await inspect_storage(table, engine=engine)
            if storage.needs_seed:
                await prepare_storage(table, engine=engine, users_to_grant=settings.sales_op_grant_select_users)
            inserted = await replace_table_rows(engine, table, rows=rows_by_source[definition.code])
        except Exception:
            failures += 1
            LOGGER.exception("Не удалось заменить данные таблицы %s.%s.", table.schema, table.name)
            continue
        loaded_tables.append(table)
        LOGGER.info("Данные таблицы %s.%s заменены: строк=%s.", table.schema, table.name, inserted)
    return tuple(loaded_tables), failures


def _classify_file(file_name: str) -> str | None:
    for definition in SOURCE_DEFINITIONS:
        if definition.matches_file_name(file_name):
            return definition.code
    return None
