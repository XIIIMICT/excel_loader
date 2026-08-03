from __future__ import annotations

import logging
import re
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app_logging import get_logger
from business_errors import BusinessLoadError
from db import create_postgres_engine
from dictionary_models import SourceHistoryEntry
from postgres import inspect_storage, replace_table_rows
from schema_definition import build_dictionary_table
from source_history import prepare_source_history
from source_resolver import SourceError, resolve_all_pattern_sources, resolve_source
from storage import prepare_storage

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine

    from config import ModuleConfig, Settings
    from dictionaries.base import CombinedDictionaryLoader, DictionaryLoader, PatternDictionaryLoader
    from dictionary_models import DictionaryPayload

LOGGER = get_logger("dictionaries")
WHITESPACE_PATTERN = re.compile(r"\s+")
NULL_MARKERS = {"(н/д)"}


@dataclass(frozen=True, slots=True)
class DictionaryRunSummary:
    failed_files: int
    warning_files: int
    loaded_tables: tuple[Table, ...]

    @property
    def has_errors(self) -> bool:
        return self.failed_files > 0

    @property
    def has_warnings(self) -> bool:
        return self.warning_files > 0


async def run_dictionaries(  # noqa: PLR0913
    settings: Settings,
    *,
    module_label: str,
    module_config: ModuleConfig,
    dictionary_loaders: tuple[DictionaryLoader, ...],
    combined_loaders: tuple[CombinedDictionaryLoader, ...] = (),
    pattern_loaders: tuple[PatternDictionaryLoader, ...] = (),
) -> DictionaryRunSummary:
    failed_files = 0
    warning_files = 0
    loaded_tables: list[Table] = []
    engine = create_postgres_engine(settings)
    try:
        history_table = await prepare_source_history(engine, users_to_grant=module_config.users_to_grant)
        for dictionary_loader in dictionary_loaders:
            result = await _process_dictionary_loader(
                settings=settings,
                module_label=module_label,
                module_config=module_config,
                dictionary_loader=dictionary_loader,
                engine=engine,
                history_table=history_table,
            )
            failed_files, warning_files, loaded_tables = _collect_loader_result(
                result=result,
                failed_files=failed_files,
                warning_files=warning_files,
                loaded_tables=loaded_tables,
            )
        for combined_loader in combined_loaders:
            result = await _process_combined_loader(
                settings=settings,
                module_label=module_label,
                module_config=module_config,
                combined_loader=combined_loader,
                engine=engine,
                history_table=history_table,
            )
            failed_files, warning_files, loaded_tables = _collect_loader_result(
                result=result,
                failed_files=failed_files,
                warning_files=warning_files,
                loaded_tables=loaded_tables,
            )
        for pattern_loader in pattern_loaders:
            result = await _process_pattern_loader(
                settings=settings,
                module_label=module_label,
                module_config=module_config,
                pattern_loader=pattern_loader,
                engine=engine,
                history_table=history_table,
            )
            failed_files, warning_files, loaded_tables = _collect_loader_result(
                result=result,
                failed_files=failed_files,
                warning_files=warning_files,
                loaded_tables=loaded_tables,
            )

    finally:
        await engine.dispose()

    return DictionaryRunSummary(
        failed_files=failed_files,
        warning_files=warning_files,
        loaded_tables=tuple(loaded_tables),
    )


def _collect_loader_result(
    *,
    result: tuple[Table | None, bool],
    failed_files: int,
    warning_files: int,
    loaded_tables: list[Table],
) -> tuple[int, int, list[Table]]:
    loaded_table, is_warning = result
    if is_warning:
        warning_files += 1
    if loaded_table is None and not is_warning:
        failed_files += 1
    elif loaded_table is not None:
        loaded_tables.append(loaded_table)
    return failed_files, warning_files, loaded_tables


async def _process_combined_loader(  # noqa: PLR0913
    *,
    settings: Settings,
    module_label: str,
    module_config: ModuleConfig,
    combined_loader: CombinedDictionaryLoader,
    engine: AsyncEngine,
    history_table: Table,
) -> tuple[Table | None, bool]:
    source_files = []
    missing_sources = 0
    try:
        with ExitStack() as stack:
            for source_path in combined_loader.source_paths:
                try:
                    source_file = stack.enter_context(
                        resolve_source(
                            settings,
                            source_config=module_config.sources,
                            data_dir=module_config.sources.data_dir,
                            default_pattern_or_path=source_path,
                        ),
                    )
                except SourceError as error:
                    LOGGER.warning(
                        "%s: файл %s не найден, его часть составного справочника %s пропущена. Причина: %s.",
                        module_label,
                        source_path,
                        combined_loader.table_name,
                        error,
                    )
                    missing_sources += 1
                    continue
                source_files.append(source_file)

            if not source_files:
                return None, True

            combined_payload = combined_loader.build_payload(
                settings,
                tuple(source_file.path for source_file in source_files),
            )
            payload = combined_payload.payload
            history_entries = tuple(
                SourceHistoryEntry(
                    loaded_at=metadata.loaded_at,
                    src_file=source_file.history_reference,
                    dest_table=payload.table_reference,
                    file_modified_at=metadata.file_modified_at,
                    file_modifier=metadata.file_modifier,
                )
                for source_file, metadata in zip(source_files, combined_payload.source_metadata, strict=True)
            )

        table = build_dictionary_table(
            table_reference=payload.table_reference,
            columns=payload.columns,
            primary_key_columns=payload.primary_key_columns,
        )
        storage = await inspect_storage(table, engine=engine)
        if storage.needs_seed:
            await prepare_storage(table, engine=engine, users_to_grant=module_config.users_to_grant)
        await replace_table_rows(
            engine,
            table,
            rows=payload.rows,
            history_table=history_table,
            history_entries=history_entries,
        )
    except BusinessLoadError as error:
        LOGGER.log(
            logging.ERROR,
            "%s: составной справочник %s не обновлён из файлов %s. Причина: %s.",
            module_label,
            combined_loader.table_name,
            ", ".join(source_file.display_name for source_file in source_files),
            error.reason,
        )
        return None, False
    except Exception:
        LOGGER.exception(
            "%s: составной справочник %s не обновлён из-за внутренней ошибки обработки.",
            module_label,
            combined_loader.table_name,
        )
        return None, False

    LOGGER.info(
        "%s: составной справочник %s успешно обновлён из %d файлов.",
        module_label,
        combined_loader.table_name,
        len(source_files),
    )
    return table, missing_sources > 0 or payload.warning_count > 0


async def _process_pattern_loader(  # noqa: PLR0913
    *,
    settings: Settings,
    module_label: str,
    module_config: ModuleConfig,
    pattern_loader: PatternDictionaryLoader,
    engine: AsyncEngine,
    history_table: Table,
) -> tuple[Table | None, bool]:
    data_dir = module_config.sources.data_dir
    if pattern_loader.source_dir:
        data_dir = data_dir / pattern_loader.source_dir

    source_files = []
    with resolve_all_pattern_sources(
        settings,
        source_config=module_config.sources,
        data_dir=data_dir,
        default_file_pattern=pattern_loader.file_pattern,
    ) as resolved_files:
        source_files = list(resolved_files)

    if not source_files:
        LOGGER.warning(
            "%s: файлы по шаблону %r для справочника %s не найдены, загрузка пропущена.",
            module_label,
            pattern_loader.file_pattern,
            pattern_loader.table_name,
        )
        return None, True

    try:
        combined_payload = pattern_loader.build_payload(
            settings,
            tuple(source_file.path for source_file in source_files),
        )
        payload = combined_payload.payload
        history_entries = tuple(
            SourceHistoryEntry(
                loaded_at=metadata.loaded_at,
                src_file=source_file.history_reference,
                dest_table=payload.table_reference,
                file_modified_at=metadata.file_modified_at,
                file_modifier=metadata.file_modifier,
            )
            for source_file, metadata in zip(source_files, combined_payload.source_metadata, strict=True)
        )

        table = build_dictionary_table(
            table_reference=payload.table_reference,
            columns=payload.columns,
            primary_key_columns=payload.primary_key_columns,
        )
        storage = await inspect_storage(table, engine=engine)
        if storage.needs_seed:
            await prepare_storage(table, engine=engine, users_to_grant=module_config.users_to_grant)
        await replace_table_rows(
            engine,
            table,
            rows=payload.rows,
            history_table=history_table,
            history_entries=history_entries,
        )
    except BusinessLoadError as error:
        LOGGER.log(
            logging.ERROR,
            "%s: справочник %s не обновлён из файлов %s. Причина: %s.",
            module_label,
            pattern_loader.table_name,
            ", ".join(source_file.display_name for source_file in source_files),
            error.reason,
        )
        return None, False
    except Exception:
        LOGGER.exception(
            "%s: справочник %s не обновлён из-за внутренней ошибки обработки.",
            module_label,
            pattern_loader.table_name,
        )
        return None, False

    LOGGER.info(
        "%s: справочник %s успешно обновлён из %d файлов.",
        module_label,
        pattern_loader.table_name,
        len(source_files),
    )
    return table, payload.warning_count > 0


async def _process_dictionary_loader(  # noqa: PLR0913
    *,
    settings: Settings,
    module_label: str,
    module_config: ModuleConfig,
    dictionary_loader: DictionaryLoader,
    engine: AsyncEngine,
    history_table: Table,
) -> tuple[Table | None, bool]:
    source_display_name = dictionary_loader.source_path
    try:
        with resolve_source(
            settings,
            source_config=module_config.sources,
            data_dir=module_config.sources.data_dir,
            default_pattern_or_path=dictionary_loader.source_path,
        ) as source_file:
            source_path = source_file.path
            source_display_name = source_file.display_name
            payload = dictionary_loader.build_payload(settings, source_path)
            metadata = payload.metadata
            history_entry = SourceHistoryEntry(
                loaded_at=metadata.loaded_at,
                src_file=source_file.history_reference,
                dest_table=payload.table_reference,
                file_modified_at=metadata.file_modified_at,
                file_modifier=metadata.file_modifier,
            )

        LOGGER.info(
            "%s: файл %s, метаданные: file_modifier=%s, file_modified_at=%s, loaded_at=%s.",
            module_label,
            source_display_name,
            metadata.file_modifier,
            metadata.file_modified_at.isoformat(),
            metadata.loaded_at.isoformat(),
        )
        payload = _normalize_payload_columns(payload, dictionary_loader=dictionary_loader)
        table = build_dictionary_table(
            table_reference=payload.table_reference,
            columns=payload.columns,
            primary_key_columns=payload.primary_key_columns,
        )
        storage = await inspect_storage(
            table,
            engine=engine,
        )
        if storage.needs_seed:
            await prepare_storage(
                table,
                engine=engine,
                users_to_grant=module_config.users_to_grant,
            )

        await replace_table_rows(
            engine,
            table,
            rows=payload.rows,
            history_table=history_table,
            history_entries=(history_entry,),
        )
    except SourceError as error:
        if "файл не найден" in str(error):
            LOGGER.warning("%s: файл %s не найден, загрузка пропущена.", module_label, source_display_name)
            return None, True
        LOGGER.log(logging.ERROR, "%s: файл %s не обновлен. Причина: %s.", module_label, source_display_name, error)
        return None, False
    except BusinessLoadError as error:
        reason = error.reason if isinstance(error, BusinessLoadError) else str(error)
        LOGGER.log(logging.ERROR, "%s: файл %s не обновлен. Причина: %s.", module_label, source_display_name, reason)
        return None, False
    except Exception:
        LOGGER.exception(
            "%s: файл %s не обновлен из-за внутренней ошибки обработки.", module_label, source_display_name
        )
        return None, False

    LOGGER.info("%s: файл %s успешно обновлен.", module_label, source_display_name)
    return table, payload.warning_count > 0


def _normalize_payload_columns(
    payload: DictionaryPayload,
    *,
    dictionary_loader: DictionaryLoader,
) -> DictionaryPayload:
    if not dictionary_loader.columns_to_normalize:
        return payload

    payload_column_names = {column.name for column in payload.columns}
    for column_name in dictionary_loader.columns_to_normalize:
        if column_name not in payload_column_names:
            LOGGER.info(
                "Справочник %s: столбец %s был настроен для нормализации, но не найден.",
                dictionary_loader.table_name,
                column_name,
            )
            continue

        for row in payload.rows:
            value = row.get(column_name)
            if isinstance(value, str):
                row[column_name] = _normalize_text_value(
                    value,
                    style=dictionary_loader.normalization_style_for(column_name),
                )
        LOGGER.info("Справочник %s: столбец %s нормализован.", dictionary_loader.table_name, column_name)

    return payload


def _normalize_text_value(value: str, *, style: str) -> str | None:
    collapsed = WHITESPACE_PATTERN.sub(" ", value)
    if collapsed != " ":
        collapsed = value.strip()
    elif not collapsed:
        return None
    if collapsed.lower() in NULL_MARKERS:
        return None
    if style == "uppercase":
        return collapsed.upper()
    return collapsed.capitalize()
