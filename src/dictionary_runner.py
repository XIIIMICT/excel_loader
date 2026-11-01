import logging
import re
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from app_logging import get_logger
from business_errors import BusinessLoadError
from db import postgres_session
from dictionary_models import SourceHistoryEntry
from module_models import ModuleOutcome
from postgres import inspect_storage, replace_table_rows
from schema_definition import build_dictionary_table
from source_history import prepare_source_history
from source_resolver import (
    SourceError,
    SourceFileNotFoundError,
    resolve_all_pattern_sources,
    resolve_source,
)
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
class _LoaderResolution:
    """
    A loader whose sources are resolved and payload is built, ready to persist.

    ``skipped_missing`` records that a combined/pattern loader had a part
    missing or carried conversion warnings — the run is still a success but
    flagged for the warning counter.
    """

    table_name: str
    display_names: tuple[str, ...]
    payload: DictionaryPayload
    history_entries: tuple[SourceHistoryEntry, ...]
    skipped_missing: bool


class _LoaderStrategy(Protocol):
    """
    One loader kind: resolve sources → build payload → history entries.

    The orchestrator calls only ``table_name`` and ``resolve``; per-kind quirks
    (single-file metadata log, combined missing-part warnings, pattern
    no-files warning) live inside ``resolve``. Returns ``None`` when the loader
    should be skipped as a WARNING (already logged).
    """

    @property
    def table_name(self) -> str: ...

    def resolve(
        self,
        settings: Settings,
        module_label: str,
        module_config: ModuleConfig,
    ) -> _LoaderResolution | None: ...


async def run_dictionaries(  # noqa: PLR0913
    settings: Settings,
    *,
    module_label: str,
    module_config: ModuleConfig,
    dictionary_loaders: tuple[DictionaryLoader, ...],
    combined_loaders: tuple[CombinedDictionaryLoader, ...] = (),
    pattern_loaders: tuple[PatternDictionaryLoader, ...] = (),
) -> ModuleOutcome:
    """
    Run every loader for one module and return a ``ModuleOutcome``.

    The three loader kinds are normalised into ``_LoaderStrategy`` adapters, so
    a new kind is a new adapter + a registration line here, not a copy of the
    pipeline. ``has_errors``/``has_warnings`` mirror the historical
    ``DictionaryRunSummary`` contract: a missing file or a load with conversion
    warnings counts as a warning; a failed load (``SourceError`` /
    ``BusinessLoadError`` / internal exception) counts as an error.
    """
    strategies: list[_LoaderStrategy] = [
        *(_SingleLoaderStrategy(loader) for loader in dictionary_loaders),
        *(_CombinedLoaderStrategy(loader) for loader in combined_loaders),
        *(_PatternLoaderStrategy(loader) for loader in pattern_loaders),
    ]

    loaded_tables: list[Table] = []
    has_errors = False
    has_warnings = False
    async with postgres_session(settings) as engine:
        history_table = await prepare_source_history(engine, users_to_grant=module_config.users_to_grant)
        for strategy in strategies:
            table, had_warning = await _run_loader_strategy(
                settings=settings,
                module_label=module_label,
                module_config=module_config,
                strategy=strategy,
                engine=engine,
                history_table=history_table,
            )
            if had_warning:
                has_warnings = True
            if table is None and not had_warning:
                has_errors = True
            elif table is not None:
                loaded_tables.append(table)

    return ModuleOutcome(
        has_errors=has_errors,
        has_warnings=has_warnings,
        has_format_changes=False,
        tables_to_grant=tuple(loaded_tables),
        relation_references_to_grant=(),
    )


async def _run_loader_strategy(  # noqa: PLR0911, PLR0913
    *,
    settings: Settings,
    module_label: str,
    module_config: ModuleConfig,
    strategy: _LoaderStrategy,
    engine: AsyncEngine,
    history_table: Table,
) -> tuple[Table | None, bool]:
    """
    Return ``(table, had_warning)``.

    ``had_warning`` is true for a clean WARNING+skip (file not found, no
    pattern files, all combined parts missing) and for a successful load that
    carried conversion warnings or skipped missing parts. A failure
    (``SourceError`` / ``BusinessLoadError`` / internal exception) returns
    ``(None, False)`` so the caller records it as an error.
    """
    try:
        resolution = strategy.resolve(settings, module_label, module_config)
    except SourceFileNotFoundError as error:
        LOGGER.warning(
            "%s: файл %s не найден, загрузка пропущена.",
            module_label,
            error.source_path,
        )
        return None, True
    except SourceError as error:
        LOGGER.log(
            logging.ERROR,
            "%s: справочник %s не обновлён. Причина: %s.",
            module_label,
            strategy.table_name,
            error,
        )
        return None, False
    except BusinessLoadError as error:
        LOGGER.log(
            logging.ERROR,
            "%s: справочник %s не обновлён. Причина: %s.",
            module_label,
            strategy.table_name,
            error.reason,
        )
        return None, False
    except Exception:
        LOGGER.exception(
            "%s: справочник %s не обновлён из-за внутренней ошибки обработки.",
            module_label,
            strategy.table_name,
        )
        return None, False

    if resolution is None:
        return None, True

    table = await _persist_payload(
        module_label=module_label,
        module_config=module_config,
        resolution=resolution,
        engine=engine,
        history_table=history_table,
    )
    if table is None:
        return None, False
    had_warning = resolution.skipped_missing or resolution.payload.warning_count > 0
    return table, had_warning


async def _persist_payload(
    *,
    module_label: str,
    module_config: ModuleConfig,
    resolution: _LoaderResolution,
    engine: AsyncEngine,
    history_table: Table,
) -> Table | None:
    payload = resolution.payload
    table = build_dictionary_table(
        table_reference=payload.table_reference,
        columns=payload.columns,
        primary_key_columns=payload.primary_key_columns,
    )
    try:
        storage = await inspect_storage(table, engine=engine)
        if storage.needs_seed:
            await prepare_storage(table, engine=engine, users_to_grant=module_config.users_to_grant)
        await replace_table_rows(
            engine,
            table,
            rows=payload.rows,
            history_table=history_table,
            history_entries=resolution.history_entries,
        )
    except BusinessLoadError as error:
        LOGGER.log(
            logging.ERROR,
            "%s: справочник %s не обновлён из файлов %s. Причина: %s.",
            module_label,
            resolution.table_name,
            ", ".join(resolution.display_names),
            error.reason,
        )
        return None
    except Exception:
        LOGGER.exception(
            "%s: справочник %s не обновлён из-за внутренней ошибки обработки.",
            module_label,
            resolution.table_name,
        )
        return None

    LOGGER.info(
        "%s: справочник %s успешно обновлён из %d файлов.",
        module_label,
        resolution.table_name,
        len(resolution.display_names),
    )
    return table


@dataclass(frozen=True, slots=True)
class _SingleLoaderStrategy:
    loader: DictionaryLoader

    @property
    def table_name(self) -> str:
        return self.loader.table_name

    def resolve(
        self,
        settings: Settings,
        module_label: str,
        module_config: ModuleConfig,
    ) -> _LoaderResolution | None:
        with resolve_source(
            settings,
            source_config=module_config.sources,
            data_dir=module_config.sources.data_dir,
            default_pattern_or_path=self.loader.source_path,
        ) as source_file:
            payload = self.loader.build_payload(settings, source_file.path)
            LOGGER.info(
                "%s: файл %s, метаданные: file_modifier=%s, file_modified_at=%s, loaded_at=%s.",
                module_label,
                source_file.display_name,
                payload.metadata.file_modifier,
                payload.metadata.file_modified_at.isoformat(),
                payload.metadata.loaded_at.isoformat(),
            )
            payload = _normalize_payload_columns(payload, dictionary_loader=self.loader)
            history_entry = SourceHistoryEntry(
                loaded_at=payload.metadata.loaded_at,
                src_file=source_file.history_reference,
                dest_table=payload.table_reference,
                file_modified_at=payload.metadata.file_modified_at,
                file_modifier=payload.metadata.file_modifier,
            )
            display_name = source_file.display_name

        return _LoaderResolution(
            table_name=self.loader.table_name,
            display_names=(display_name,),
            payload=payload,
            history_entries=(history_entry,),
            skipped_missing=False,
        )


@dataclass(frozen=True, slots=True)
class _CombinedLoaderStrategy:
    loader: CombinedDictionaryLoader

    @property
    def table_name(self) -> str:
        return self.loader.table_name

    def resolve(
        self,
        settings: Settings,
        module_label: str,
        module_config: ModuleConfig,
    ) -> _LoaderResolution | None:
        source_files: list = []
        missing_sources = 0
        with ExitStack() as stack:
            for source_path in self.loader.source_paths:
                try:
                    source_file = stack.enter_context(
                        resolve_source(
                            settings,
                            source_config=module_config.sources,
                            data_dir=module_config.sources.data_dir,
                            default_pattern_or_path=source_path,
                        ),
                    )
                except SourceFileNotFoundError as error:
                    LOGGER.warning(
                        "%s: файл %s не найден, его часть составного справочника %s пропущена. Причина: %s.",
                        module_label,
                        error.source_path,
                        self.loader.table_name,
                        error,
                    )
                    missing_sources += 1
                    continue
                source_files.append(source_file)

            if not source_files:
                LOGGER.warning(
                    "%s: составной справочник %s пропущен — ни одна часть не найдена.",
                    module_label,
                    self.loader.table_name,
                )
                return None

            combined_payload = self.loader.build_payload(
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
            display_names = tuple(source_file.display_name for source_file in source_files)

        return _LoaderResolution(
            table_name=self.loader.table_name,
            display_names=display_names,
            payload=payload,
            history_entries=history_entries,
            skipped_missing=missing_sources > 0,
        )


@dataclass(frozen=True, slots=True)
class _PatternLoaderStrategy:
    loader: PatternDictionaryLoader

    @property
    def table_name(self) -> str:
        return self.loader.table_name

    def resolve(
        self,
        settings: Settings,
        module_label: str,
        module_config: ModuleConfig,
    ) -> _LoaderResolution | None:
        data_dir = module_config.sources.data_dir
        if self.loader.source_dir:
            data_dir = data_dir / self.loader.source_dir

        with resolve_all_pattern_sources(
            settings,
            source_config=module_config.sources,
            data_dir=data_dir,
            default_file_pattern=self.loader.file_pattern,
        ) as resolved_files:
            source_files = list(resolved_files)

        if not source_files:
            LOGGER.warning(
                "%s: файлы по шаблону %r для справочника %s не найдены, загрузка пропущена.",
                module_label,
                self.loader.file_pattern,
                self.loader.table_name,
            )
            return None

        combined_payload = self.loader.build_payload(
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
        return _LoaderResolution(
            table_name=self.loader.table_name,
            display_names=tuple(source_file.display_name for source_file in source_files),
            payload=payload,
            history_entries=history_entries,
            skipped_missing=False,
        )


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
    if not collapsed:
        return None
    if collapsed.lower() in NULL_MARKERS:
        return None
    if style == "uppercase":
        return collapsed.upper()
    return collapsed.capitalize()
