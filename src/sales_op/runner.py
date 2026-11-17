import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol

from app_logging import get_logger
from business_errors import BusinessLoadError
from db import postgres_session
from dictionary_models import SourceHistoryEntry
from module_models import ModuleOutcome
from postgres import inspect_storage
from sales_op.db import build_sales_op_table, create_sales_op_view, replace_month_rows
from sales_op.excel_parser import inspect_sales_op_workbook, parse_sales_op_workbook
from sales_op.models import SalesOpError, SalesOpRunOptions, SelectedSalesOpFile
from sales_op.specification import SPECIFICATION_FILE_NAME, read_sales_op_specification, source_table_name
from source_history import prepare_source_history
from source_resolver import SourceError, resolve_all_pattern_sources
from storage import prepare_storage

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import tzinfo
    from pathlib import Path

    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine

    from config import ModuleConfig, Settings
    from dictionary_models import DbRow
    from sales_op.models import SalesOpSourceSpecification, SalesOpSpecification
    from source_resolver import SourceFile

LOGGER = get_logger("sales_op")
DEFAULT_FILE_PATTERN = r".*\.xlsx$"
MONTH_FOLDER_PATTERN = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")
ALL_MONTHS_LABEL = "all"

# source code + first day of the month folder
type MonthGroupKey = tuple[str, date]


class _FileEntry(Protocol):
    """Minimal file descriptor shared by ``SourceFile`` and ``SelectedSalesOpFile``."""

    @property
    def path(self) -> Path: ...


@dataclass(frozen=True, slots=True)
class _FileSelection:
    files_by_group: dict[MonthGroupKey, tuple[SelectedSalesOpFile, ...]]
    failed_groups: frozenset[MonthGroupKey]
    missing_groups: frozenset[MonthGroupKey]
    warnings: int
    failures: int


@dataclass(frozen=True, slots=True)
class _ParsedFiles:
    rows_by_group: dict[MonthGroupKey, list[DbRow]]
    history_by_group: dict[MonthGroupKey, list[SourceHistoryEntry]]
    warnings: int
    failures: int


def parse_sales_op_options(*, force: bool, months: str | None) -> SalesOpRunOptions:
    """
    Parse ``--force``/``--months`` into run options.

    ``months`` is a comma-separated list of ``YYYY-MM`` folder names or the
    literal ``all``; ``None`` means the current month (resolved by the runner).
    """
    if months is None:
        return SalesOpRunOptions(force=force)

    raw_months = months.strip()
    if not raw_months:
        message = "--months: пустой список месяцев."
        raise ValueError(message)
    if raw_months.casefold() == ALL_MONTHS_LABEL:
        return SalesOpRunOptions(force=force, all_months=True)

    selected_months: list[date] = []
    for raw_month in raw_months.split(","):
        month_label = raw_month.strip()
        match = MONTH_FOLDER_PATTERN.fullmatch(month_label)
        if match is None:
            message = f"--months: некорректная папка месяца {month_label!r}, ожидается формат ГГГГ-ММ."
            raise ValueError(message)
        try:
            month_start = date(int(match.group("year")), int(match.group("month")), 1)
        except ValueError as error:
            message = f"--months: некорректная папка месяца {month_label!r}: {error}."
            raise ValueError(message) from error
        if month_start not in selected_months:
            selected_months.append(month_start)
    return SalesOpRunOptions(force=force, months=tuple(selected_months))


async def run_sales_op(settings: Settings, *, options: SalesOpRunOptions) -> ModuleOutcome:
    module = settings.modules["sales_op"]
    data_dir = module.sources.data_dir
    if module.sources.source != "local":
        LOGGER.log(
            40,
            "План продаж не загружен. Модуль читает только локальные папки месяцев, источник %r не поддерживается.",
            module.sources.source,
        )
        return _sales_op_outcome(has_errors=True, loaded_tables=(), view_reference=None, has_warnings=False)

    try:
        specification = read_sales_op_specification(data_dir / SPECIFICATION_FILE_NAME)
    except SalesOpError as error:
        LOGGER.log(40, "План продаж не загружен. Причина: %s.", error)
        return _sales_op_outcome(has_errors=True, loaded_tables=(), view_reference=None, has_warnings=False)

    try:
        month_folders = _select_month_folders(data_dir, options=options, timezone=settings.app_timezone)
        month_files = _collect_month_files(settings, module=module, month_folders=month_folders, force=options.force)
    except (SourceError, SalesOpError) as error:
        LOGGER.log(40, "План продаж не загружен. Причина: %s.", error)
        return _sales_op_outcome(has_errors=True, loaded_tables=(), view_reference=None, has_warnings=False)

    selection = _select_source_files(
        month_files,
        specification_sources=specification.sources,
        timezone=settings.app_timezone,
    )
    parsed = _parse_selected_files(
        selection.files_by_group,
        failed_groups=selection.failed_groups,
        specification_sources=specification.sources,
    )

    async with postgres_session(settings) as engine:
        loaded_groups, loaded_tables, storage_failures = await _replace_tables(
            settings,
            engine=engine,
            specification=specification,
            rows_by_group=parsed.rows_by_group,
            history_by_group=parsed.history_by_group,
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

    _log_missing_data(settings, missing_groups=selection.missing_groups)
    rename_failures = _mark_processed_files(selection.files_by_group, loaded_groups=loaded_groups)

    return _sales_op_outcome(
        has_errors=selection.failures + parsed.failures + storage_failures + view_failures > 0,
        loaded_tables=loaded_tables,
        view_reference=view_reference,
        has_warnings=selection.warnings + parsed.warnings + rename_failures > 0,
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


def _select_month_folders(data_dir: Path, *, options: SalesOpRunOptions, timezone: tzinfo) -> tuple[date, ...]:
    if options.all_months:
        month_folders = _available_month_folders(data_dir)
        if not month_folders:
            message = f"в каталоге {data_dir} не найдены папки месяцев вида ГГГГ-ММ"
            raise SalesOpError(message)
        return month_folders

    months = options.months
    if months is None:
        months = (datetime.now(tz=timezone).date().replace(day=1),)
    for month_start in months:
        month_folder = data_dir / _month_folder_name(month_start)
        if not month_folder.is_dir():
            message = f"папка месяца не найдена: {month_folder}"
            raise SalesOpError(message)
    return months


def _available_month_folders(data_dir: Path) -> tuple[date, ...]:
    if not data_dir.is_dir():
        return ()
    month_folders: list[date] = []
    for path in sorted(data_dir.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_dir():
            continue
        month_start = _parse_month_folder_name(path.name)
        if month_start is not None:
            month_folders.append(month_start)
    return tuple(month_folders)


def _parse_month_folder_name(folder_name: str) -> date | None:
    match = MONTH_FOLDER_PATTERN.fullmatch(folder_name)
    if match is None:
        return None
    try:
        return date(int(match.group("year")), int(match.group("month")), 1)
    except ValueError:
        return None


def _month_folder_name(month_start: date) -> str:
    return f"{month_start.year:04d}-{month_start.month:02d}"


def _collect_month_files(
    settings: Settings,
    *,
    module: ModuleConfig,
    month_folders: tuple[date, ...],
    force: bool,
) -> tuple[tuple[date, tuple[SourceFile, ...]], ...]:
    collected: list[tuple[date, tuple[SourceFile, ...]]] = []
    for month_start in month_folders:
        month_folder = module.sources.data_dir / _month_folder_name(month_start)
        with resolve_all_pattern_sources(
            settings,
            source_config=module.sources,
            data_dir=month_folder,
            default_file_pattern=DEFAULT_FILE_PATTERN,
            recursive_local=True,
        ) as source_files:
            selected_files = tuple(
                source_file for source_file in source_files if force or not source_file.path.name.startswith("_")
            )
        collected.append((month_start, selected_files))
    return tuple(collected)


def _select_source_files(
    month_files: tuple[tuple[date, tuple[SourceFile, ...]], ...],
    *,
    specification_sources: dict[str, SalesOpSourceSpecification],
    timezone: tzinfo,
) -> _FileSelection:
    candidates: dict[MonthGroupKey, list[SelectedSalesOpFile]] = defaultdict(list)
    failed_groups: set[MonthGroupKey] = set()
    missing_groups: set[MonthGroupKey] = set()
    warnings = 0
    failures = 0
    for month_start, source_files in month_files:
        month_candidates, month_failed_groups, sources_with_candidates, month_failures = _collect_month_candidates(
            month_start,
            source_files,
            specification_sources=specification_sources,
            timezone=timezone,
        )
        for group_key, group_candidates in month_candidates.items():
            candidates[group_key].extend(group_candidates)
        failed_groups.update(month_failed_groups)
        failures += month_failures
        month_missing = _missing_source_groups(
            month_start,
            sources_with_candidates=sources_with_candidates,
            specification_sources=specification_sources,
        )
        missing_groups.update(month_missing)
        warnings += len(month_missing)

    files_by_group, selection_warnings = _choose_latest_files(candidates)
    return _FileSelection(
        files_by_group=files_by_group,
        failed_groups=frozenset(failed_groups),
        missing_groups=frozenset(missing_groups),
        warnings=warnings + selection_warnings,
        failures=failures,
    )


def _collect_month_candidates(
    month_start: date,
    source_files: tuple[SourceFile, ...],
    *,
    specification_sources: dict[str, SalesOpSourceSpecification],
    timezone: tzinfo,
) -> tuple[dict[MonthGroupKey, list[SelectedSalesOpFile]], set[MonthGroupKey], set[str], int]:
    candidates: dict[MonthGroupKey, list[SelectedSalesOpFile]] = defaultdict(list)
    failed_groups: set[MonthGroupKey] = set()
    sources_with_candidates: set[str] = set()
    failures = 0
    for source_file in source_files:
        original_name = source_file.original_name or source_file.path.name
        source_code = _classify_file(original_name, specification_sources=specification_sources)
        if source_code is None:
            continue
        sources_with_candidates.add(source_code)
        group_key = (source_code, month_start)
        candidate = _inspect_candidate(
            source_file,
            source_code=source_code,
            original_name=original_name,
            month_start=month_start,
            timezone=timezone,
        )
        if candidate is None:
            failures += 1
            failed_groups.add(group_key)
            continue
        candidates[group_key].append(candidate)
    return dict(candidates), failed_groups, sources_with_candidates, failures


def _inspect_candidate(
    source_file: SourceFile,
    *,
    source_code: str,
    original_name: str,
    month_start: date,
    timezone: tzinfo,
) -> SelectedSalesOpFile | None:
    try:
        plan_date, modified_at, metadata = inspect_sales_op_workbook(source_file.path, timezone=timezone)
    except Exception:
        LOGGER.exception(
            "План продаж %s за %s: не удалось определить дату файла %s; источник за месяц не загружается.",
            source_code,
            _month_folder_name(month_start),
            source_file.display_name,
        )
        return None
    if (plan_date.year, plan_date.month) != (month_start.year, month_start.month):
        LOGGER.error(
            "План продаж %s за %s: файл %s содержит дату %s; источник за месяц не загружается.",
            source_code,
            _month_folder_name(month_start),
            source_file.display_name,
            plan_date,
        )
        return None
    return SelectedSalesOpFile(
        source_code=source_code,
        path=source_file.path,
        display_name=source_file.display_name,
        original_name=original_name,
        plan_date=plan_date,
        modified_at=modified_at,
        metadata=metadata,
        history_reference=source_file.history_reference,
    )


def _missing_source_groups(
    month_start: date,
    *,
    sources_with_candidates: set[str],
    specification_sources: dict[str, SalesOpSourceSpecification],
) -> set[MonthGroupKey]:
    """
    Source/month pairs with no matching file at all.

    The run reports them once, after the table-replacement summary, as
    "no data for <dest_table> for <month>" - the same information used to be a
    scan-time warning, but the end-of-run report is where the loader's result
    is read.
    """
    return {
        (source_code, month_start)
        for source_code in specification_sources
        if source_code not in sources_with_candidates
    }


def _choose_latest_files(
    candidates: dict[MonthGroupKey, list[SelectedSalesOpFile]],
) -> tuple[dict[MonthGroupKey, tuple[SelectedSalesOpFile, ...]], int]:
    warnings = 0
    files_by_group: dict[MonthGroupKey, tuple[SelectedSalesOpFile, ...]] = {}
    for group_key, group_candidates in candidates.items():
        by_date: dict[date, list[SelectedSalesOpFile]] = defaultdict(list)
        for candidate in group_candidates:
            by_date[candidate.plan_date].append(candidate)
        selected_files: list[SelectedSalesOpFile] = []
        for plan_date in sorted(by_date):
            date_candidates = by_date[plan_date]
            winner = max(date_candidates, key=lambda item: (item.modified_at, item.original_name.casefold()))
            selected_files.append(winner)
            if len(date_candidates) > 1:
                warnings += 1
                LOGGER.warning(
                    "План продаж %s за %s: для даты %s найдено несколько файлов, выбран самый поздний: %s.",
                    group_key[0],
                    _month_folder_name(group_key[1]),
                    plan_date,
                    winner.display_name,
                )
        files_by_group[group_key] = tuple(selected_files)
    return files_by_group, warnings


def _parse_selected_files(
    files_by_group: dict[MonthGroupKey, tuple[SelectedSalesOpFile, ...]],
    *,
    failed_groups: frozenset[MonthGroupKey],
    specification_sources: dict[str, SalesOpSourceSpecification],
) -> _ParsedFiles:
    rows_by_group: dict[MonthGroupKey, list[DbRow]] = {}
    history_by_group: dict[MonthGroupKey, list[SourceHistoryEntry]] = {}
    warnings = 0
    failures = 0
    for group_key in sorted(files_by_group, key=lambda item: (item[1], item[0])):
        source_code, month_start = group_key
        if group_key in failed_groups:
            LOGGER.error(
                "План продаж %s за %s: месяц не загружен, так как хотя бы один файл источника не прошёл проверку.",
                source_code,
                _month_folder_name(month_start),
            )
            continue

        source_specification = specification_sources[source_code]
        group_rows: list[DbRow] = []
        group_history: list[SourceHistoryEntry] = []
        group_failed = False
        for selected_file in files_by_group[group_key]:
            try:
                parsed = parse_sales_op_workbook(
                    selected_file.path,
                    source_specification=source_specification,
                )
            except Exception:
                failures += 1
                group_failed = True
                LOGGER.exception(
                    "План продаж %s за %s: файл %s не обработан; источник за месяц не загружается.",
                    source_code,
                    _month_folder_name(month_start),
                    selected_file.display_name,
                )
                continue
            group_rows.extend(parsed.rows)
            metadata = selected_file.metadata
            group_history.append(
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
                "План продаж %s за %s: файл %s распознан: лист=%s, дата=%s, строк=%s.",
                source_code,
                _month_folder_name(month_start),
                selected_file.display_name,
                parsed.sheet_name,
                parsed.plan_date,
                len(parsed.rows),
            )
        if group_failed:
            continue
        rows_by_group[group_key] = group_rows
        history_by_group[group_key] = group_history

    return _ParsedFiles(
        rows_by_group=rows_by_group,
        history_by_group=history_by_group,
        warnings=warnings,
        failures=failures,
    )


async def _replace_tables(
    settings: Settings,
    *,
    engine: AsyncEngine,
    specification: SalesOpSpecification,
    rows_by_group: dict[MonthGroupKey, list[DbRow]],
    history_by_group: dict[MonthGroupKey, list[SourceHistoryEntry]],
) -> tuple[frozenset[MonthGroupKey], tuple[Table, ...], int]:
    loaded_groups: set[MonthGroupKey] = set()
    loaded_tables: list[Table] = []
    loaded_table_keys: set[tuple[str, str]] = set()
    failures = 0
    history_table = await prepare_source_history(engine, users_to_grant=settings.modules["sales_op"].users_to_grant)
    tables_by_source: dict[str, Table] = {}
    for group_key in sorted(rows_by_group, key=lambda item: (item[1], item[0])):
        source_code, month_start = group_key
        table = tables_by_source.get(source_code)
        if table is None:
            table = build_sales_op_table(
                table_reference=settings.modules["sales_op"].table_reference(source_table_name(source_code)),
                columns=specification.sources[source_code].table_columns,
            )
        entries = tuple(
            SourceHistoryEntry(
                loaded_at=entry.loaded_at,
                src_file=entry.src_file,
                dest_table=f"{table.schema}.{table.name}",
                file_modified_at=entry.file_modified_at,
                file_modifier=entry.file_modifier,
            )
            for entry in history_by_group[group_key]
        )
        try:
            if source_code not in tables_by_source:
                storage = await inspect_storage(table, engine=engine)
                if storage.needs_seed:
                    await prepare_storage(
                        table,
                        engine=engine,
                        users_to_grant=settings.modules["sales_op"].users_to_grant,
                    )
                tables_by_source[source_code] = table
            inserted = await replace_month_rows(
                engine,
                table,
                month_start=month_start,
                rows=rows_by_group[group_key],
                history_table=history_table,
                history_entries=entries,
            )
        except BusinessLoadError as error:
            failures += 1
            LOGGER.log(
                40,
                "Не удалось заменить данные таблицы %s.%s за %s. Причина: %s.",
                table.schema,
                table.name,
                _month_folder_name(month_start),
                error.reason,
            )
            continue
        except Exception:
            failures += 1
            LOGGER.exception(
                "Не удалось заменить данные таблицы %s.%s за %s.",
                table.schema,
                table.name,
                _month_folder_name(month_start),
            )
            continue
        table_key = (table.schema or "public", table.name)
        if table_key not in loaded_table_keys:
            loaded_table_keys.add(table_key)
            loaded_tables.append(table)
        loaded_groups.add(group_key)
        LOGGER.info(
            "Данные таблицы %s.%s за %s заменены: строк=%s.",
            table.schema,
            table.name,
            _month_folder_name(month_start),
            inserted,
        )
    return frozenset(loaded_groups), tuple(loaded_tables), failures


def _log_missing_data(settings: Settings, *, missing_groups: frozenset[MonthGroupKey]) -> None:
    """
    End-of-run report for source/month pairs that produced no file.

    Emitted after the table-replacement lines so the summary reads top-down:
    replaced tables first, then the pairs with nothing to load.
    """
    module = settings.modules["sales_op"]
    for source_code, month_start in sorted(missing_groups, key=lambda item: (item[1], item[0])):
        LOGGER.warning(
            "Не найдено данных для %s за %s.",
            module.table_reference(source_table_name(source_code)),
            _month_folder_name(month_start),
        )


def _mark_processed_files(
    files_by_group: Mapping[MonthGroupKey, Sequence[_FileEntry]],
    *,
    loaded_groups: frozenset[MonthGroupKey],
) -> int:
    """
    Rename the files of successfully replaced source/month groups to ``_<name>``.

    The underscore marks a file as already loaded: discovery skips it unless
    ``--force`` is given. Files that already carry the prefix are left alone.
    A failed rename (the file is open in Excel, the target already exists, the
    mount is not writable) never fails the load: the files are collected and
    reported as one WARNING, and the next run simply reloads them.
    """
    failures: list[tuple[Path, str]] = []
    for group_key in sorted(loaded_groups, key=lambda item: (item[1], item[0])):
        for selected_file in files_by_group[group_key]:
            source_path = selected_file.path
            if source_path.name.startswith("_"):
                continue
            renamed_path = source_path.with_name(f"_{source_path.name}")
            if renamed_path.exists():
                failures.append((source_path, f"целевой файл уже существует: {renamed_path.name}"))
                continue
            try:
                source_path.rename(renamed_path)
            except OSError as error:
                failures.append((source_path, str(error)))
                continue
            LOGGER.info("План продаж: обработанный файл %s переименован в %s.", source_path.name, renamed_path.name)

    if failures:
        LOGGER.warning(
            "План продаж: не удалось переименовать обработанные файлы (%d): %s. Первая причина: %s.",
            len(failures),
            ", ".join(f"{path.parent.name}/{path.name}" for path, _reason in failures),
            failures[0][1],
        )
    return len(failures)


def _classify_file(file_name: str, *, specification_sources: dict[str, SalesOpSourceSpecification]) -> str | None:
    for source_code, source_specification in specification_sources.items():
        if source_specification.matches_file_name(file_name):
            return source_code
    return None
