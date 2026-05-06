import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app_logging import get_logger
from business_errors import BusinessLoadError
from db import create_postgres_engine
from excel_parser import read_workbook_metadata
from nsi_loaders.registry import DICTIONARY_LOADERS
from postgres import inspect_storage, replace_table_rows
from schema_definition import build_dictionary_table
from source_resolver import SourceError, resolve_source
from storage import prepare_storage

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine

    from config import Settings
    from dictionary_models import DictionaryPayload
    from nsi_loaders.base import DictionaryLoader

LOGGER = get_logger("nsi")
WHITESPACE_PATTERN = re.compile(r"\s+")
NULL_MARKERS = {"(н/д)"}


@dataclass(frozen=True, slots=True)
class NsiRunSummary:
    failed_files: int
    warning_files: int
    loaded_tables: tuple[Table, ...]

    @property
    def has_errors(self) -> bool:
        return self.failed_files > 0

    @property
    def has_warnings(self) -> bool:
        return self.warning_files > 0


async def run_nsi(settings: Settings) -> NsiRunSummary:
    failed_files = 0
    warning_files = 0
    loaded_tables: list[Table] = []
    engine = create_postgres_engine(settings)
    try:
        for dictionary_loader in DICTIONARY_LOADERS:
            loaded_table, is_warning = await _process_dictionary_loader(
                settings=settings,
                dictionary_loader=dictionary_loader,
                engine=engine,
            )
            if is_warning:
                warning_files += 1
            if loaded_table is None and not is_warning:
                failed_files += 1
            elif loaded_table is not None:
                loaded_tables.append(loaded_table)

    finally:
        await engine.dispose()

    return NsiRunSummary(
        failed_files=failed_files,
        warning_files=warning_files,
        loaded_tables=tuple(loaded_tables),
    )


async def _process_dictionary_loader(
    *,
    settings: Settings,
    dictionary_loader: DictionaryLoader,
    engine: AsyncEngine,
) -> tuple[Table | None, bool]:
    source_display_name = dictionary_loader.source_path
    try:
        with resolve_source(
            settings,
            source_config=settings.nsi.sources,
            data_dir=settings.nsi.sources.data_dir,
            default_pattern_or_path=dictionary_loader.source_path,
        ) as source_file:
            source_path = source_file.path
            source_display_name = source_file.display_name
            metadata = read_workbook_metadata(source_path, timezone=settings.app_timezone)
            payload = dictionary_loader.build_payload(settings, source_path)

        LOGGER.info(
            "НСИ: файл %s, метаданные: user_name=%s, date_modified=%s, date_upload=%s.",
            source_display_name,
            metadata.user_name,
            metadata.date_modified.isoformat(),
            metadata.date_upload.isoformat(),
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
                users_to_grant=settings.nsi_grant_select_users,
            )

        await replace_table_rows(
            engine,
            table,
            rows=payload.rows,
        )
    except SourceError as error:
        if "файл не найден" in str(error):
            LOGGER.warning("НСИ: файл %s не найден, загрузка справочника пропущена.", source_display_name)
            return None, True
        LOGGER.log(logging.ERROR, "НСИ: файл %s не обновлен. Причина: %s.", source_display_name, error)
        return None, False
    except BusinessLoadError as error:
        reason = error.reason if isinstance(error, BusinessLoadError) else str(error)
        LOGGER.log(logging.ERROR, "НСИ: файл %s не обновлен. Причина: %s.", source_display_name, reason)
        return None, False
    except Exception:
        LOGGER.exception("НСИ: файл %s не обновлен из-за внутренней ошибки обработки.", source_display_name)
        return None, False

    LOGGER.info("НСИ: файл %s успешно обновлен.", source_display_name)
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
                "НСИ %s: столбец %s был настроен для нормализации, но не найден.",
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
        LOGGER.info("НСИ %s: столбец %s нормализован.", dictionary_loader.table_name, column_name)

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
