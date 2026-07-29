import re
from datetime import date, datetime, time, tzinfo
from numbers import Number
from typing import TYPE_CHECKING, Literal
from zipfile import BadZipFile, ZipFile

import pandas as pd
from defusedxml.ElementTree import ParseError, fromstring
from openpyxl import load_workbook

from app_logging import get_logger
from business_errors import (
    CellTypeConversionError,
    DuplicatePrimaryKeyError,
    FileMetadataMissingError,
    SourceColumnMissingError,
    SourceFileNotFoundError,
)
from dictionary_models import ColumnDefinition, ColumnKind, DbRow, DbValue, DictionaryPayload, WorkbookMetadata

if TYPE_CHECKING:
    from pathlib import Path

TEXT_COLUMN_KIND = "text"
TIMESTAMP_TZ_COLUMN_KIND = "timestamp_tz"
XLSB_SUFFIX = ".xlsb"
OOXML_CORE_PROPERTIES_PATH = "docProps/core.xml"
OOXML_NAMESPACES = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
}
METADATA_COLUMN_DEFINITIONS = (
    ColumnDefinition(name="date_upload", kind=TIMESTAMP_TZ_COLUMN_KIND, nullable=False),
    ColumnDefinition(name="date_modified", kind=TIMESTAMP_TZ_COLUMN_KIND, nullable=False),
)
EXCEL_ERROR_VALUES = {"#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A", "#GETTING_DATA"}
LOGGER = get_logger("excel")


def read_workbook_metadata(source_path: Path, *, timezone: tzinfo) -> WorkbookMetadata:
    if not source_path.exists():
        raise SourceFileNotFoundError(source_path)

    if source_path.suffix.lower() == XLSB_SUFFIX:
        file_modifier = _read_xlsb_user_name(source_path)
    else:
        file_modifier = _read_openpyxl_user_name(source_path)

    return WorkbookMetadata(
        file_modifier=file_modifier,
        file_modified_at=datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone),
        loaded_at=datetime.now(tz=timezone),
    )


def _read_openpyxl_user_name(source_path: Path) -> str:
    workbook = load_workbook(source_path, read_only=True, data_only=True)
    try:
        return _extract_user_name(
            last_modified_by=workbook.properties.lastModifiedBy,
            creator=workbook.properties.creator,
        )
    finally:
        workbook.close()


def _read_xlsb_user_name(source_path: Path) -> str:
    try:
        with ZipFile(source_path) as workbook_archive:
            core_properties = workbook_archive.read(OOXML_CORE_PROPERTIES_PATH)
    except (BadZipFile, KeyError) as error:
        raise FileMetadataMissingError from error

    try:
        root = fromstring(core_properties)
    except ParseError as error:
        raise FileMetadataMissingError from error

    last_modified_by = root.findtext("cp:lastModifiedBy", namespaces=OOXML_NAMESPACES)
    creator = root.findtext("dc:creator", namespaces=OOXML_NAMESPACES)
    return _extract_user_name(last_modified_by=last_modified_by, creator=creator)


def read_single_sheet_frame(
    source_path: Path,
    *,
    timezone: tzinfo,
    sheet_name: str | int = 0,
) -> tuple[pd.DataFrame, WorkbookMetadata]:
    metadata = read_workbook_metadata(source_path, timezone=timezone)
    frame = pd.read_excel(
        source_path,
        sheet_name=sheet_name,
        keep_default_na=False,
        na_filter=False,
        engine=_excel_engine(source_path),
    )
    return frame, metadata


def select_columns_between(
    frame: pd.DataFrame,
    *,
    start_column: str,
    end_column: str,
) -> pd.DataFrame:
    headers = tuple(str(column) for column in frame.columns)
    if start_column not in headers:
        raise SourceColumnMissingError(start_column)
    if end_column not in headers:
        raise SourceColumnMissingError(end_column)

    start_index = headers.index(start_column)
    end_index = headers.index(end_column)
    selected_headers = headers[start_index : end_index + 1]
    return frame.loc[:, list(selected_headers)].copy()


def select_columns_excluding_checks(frame: pd.DataFrame) -> pd.DataFrame:
    selected_headers = [str(column) for column in frame.columns if not _is_check_column(str(column))]
    return frame.loc[:, selected_headers].copy()


def build_dictionary_payload(  # noqa: C901, PLR0912, PLR0913
    *,
    table_reference: str,
    selected_frame: pd.DataFrame,
    primary_key_columns: tuple[str, ...],
    metadata: WorkbookMetadata,
    not_null_filtered_columns: tuple[str, ...] = (),
    not_null_columns: tuple[str, ...] = (),
    unique_columns: tuple[str, ...] = (),
    column_kinds: dict[str, ColumnKind] | None = None,
    source_name: str | None = None,
    preserve_text_values: bool = False,
) -> DictionaryPayload:
    selected_columns = tuple(str(column) for column in selected_frame.columns)
    for primary_key_column in primary_key_columns:
        if primary_key_column not in selected_columns:
            raise SourceColumnMissingError(primary_key_column)
    for filtered_column in not_null_filtered_columns:
        if filtered_column not in selected_columns:
            raise SourceColumnMissingError(filtered_column)
    for configured_column in (*not_null_columns, *unique_columns):
        if configured_column not in selected_columns:
            raise SourceColumnMissingError(configured_column)

    rows: list[DbRow] = []
    warning_count = 0
    unique_keys: set[tuple[object, ...]] = set()
    configured_column_kinds = column_kinds or {}
    for row_number, row in enumerate(selected_frame.itertuples(index=False, name=None), start=2):
        raw_row = dict(zip(selected_columns, row, strict=True))
        if _should_skip_raw_filtered_row(raw_row, not_null_filtered_columns):
            continue
        prepared_row: DbRow = {}
        for column_index, (column_name, value) in enumerate(zip(selected_columns, row, strict=True), start=1):
            kind = configured_column_kinds.get(column_name, TEXT_COLUMN_KIND)
            nullable = column_name not in primary_key_columns and column_name not in not_null_columns
            cell_reference = f"{_excel_column_name(column_index)}{row_number}"
            if _is_excel_error(value):
                warning_count += 1
            prepared_row[column_name] = convert_excel_value(
                value,
                kind=kind,
                cell_reference=cell_reference,
                excel_errors_as_null=True,
                source_name=source_name,
                preserve_text=preserve_text_values,
                nullable=nullable,
            )
        if _should_skip_filtered_row(prepared_row, not_null_filtered_columns):
            continue

        prepared_row["date_upload"] = metadata.loaded_at
        prepared_row["date_modified"] = metadata.file_modified_at
        if primary_key_columns:
            primary_key_value = tuple(prepared_row[column_name] for column_name in primary_key_columns)
            if primary_key_value in unique_keys:
                raise DuplicatePrimaryKeyError(primary_key_columns, row_number)
            unique_keys.add(primary_key_value)
        rows.append(prepared_row)

    columns = _build_column_definitions(
        selected_columns=selected_columns,
        primary_key_columns=primary_key_columns,
        column_kinds=configured_column_kinds,
        not_null_columns=not_null_columns,
        unique_columns=unique_columns,
    )
    return DictionaryPayload(
        table_reference=table_reference,
        primary_key_columns=primary_key_columns,
        columns=columns,
        rows=rows,
        metadata=metadata,
        warning_count=warning_count,
    )


def _should_skip_filtered_row(row: DbRow, filtered_columns: tuple[str, ...]) -> bool:
    if not filtered_columns:
        return False
    return all(_is_null_like(row[column_name]) for column_name in filtered_columns)


def _should_skip_raw_filtered_row(row: dict[str, object], filtered_columns: tuple[str, ...]) -> bool:
    return bool(filtered_columns) and all(
        normalize_text_value(row[column_name]) is None for column_name in filtered_columns
    )


def _is_null_like(value: object) -> bool:
    return value is None or value == ""


def _build_column_definitions(
    *,
    selected_columns: tuple[str, ...],
    primary_key_columns: tuple[str, ...],
    column_kinds: dict[str, ColumnKind],
    not_null_columns: tuple[str, ...],
    unique_columns: tuple[str, ...],
) -> tuple[ColumnDefinition, ...]:
    data_columns = tuple(
        ColumnDefinition(
            name=column_name,
            kind=column_kinds.get(column_name, TEXT_COLUMN_KIND),
            nullable=column_name not in primary_key_columns and column_name not in not_null_columns,
            unique=column_name in unique_columns,
        )
        for column_name in selected_columns
    )
    return (*data_columns, *METADATA_COLUMN_DEFINITIONS)


def _extract_user_name(*, last_modified_by: str | None, creator: str | None) -> str:
    for candidate in (last_modified_by, creator):
        if candidate is not None and candidate.strip():
            return candidate.strip()

    raise FileMetadataMissingError


def normalize_text_value(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    normalized = re.sub(r"\s+", " ", str(value)).strip()
    if not normalized or normalized == "-":
        return None
    return normalized


def convert_excel_value(  # noqa: PLR0913
    value: object,
    *,
    kind: ColumnKind,
    cell_reference: str,
    excel_errors_as_null: bool = False,
    source_name: str | None = None,
    preserve_text: bool = False,
    nullable: bool = True,
) -> DbValue:
    excel_error_result = _excel_error_as_null(
        value,
        enabled=excel_errors_as_null,
        source_name=source_name,
        cell_reference=cell_reference,
    )
    if excel_error_result:
        return None
    if preserve_text and kind == "text":
        return _preserve_text_value(value)
    if kind == "bool":
        return _convert_boolean_value(value, nullable=nullable)
    normalized = normalize_text_value(value)
    if normalized is None:
        return None
    return _convert_normalized_value(value, normalized=normalized, kind=kind, cell_reference=cell_reference)


def _convert_normalized_value(
    value: object,
    *,
    normalized: str,
    kind: ColumnKind,
    cell_reference: str,
) -> DbValue:
    if kind == "text":
        return normalized

    try:
        if kind == "int":
            numeric_value = float(normalized.replace(" ", "").replace(",", "."))
            if not numeric_value.is_integer():
                _raise_non_integer_value()
            return int(numeric_value)
        if kind == "float":
            return float(normalized.replace(" ", "").replace(",", "."))
        converted_datetime = _convert_datetime_value(value, normalized=normalized)
        if kind == "date":
            return converted_datetime.date()
        result = converted_datetime
    except (TypeError, ValueError, OverflowError) as error:
        raise CellTypeConversionError(cell_reference, kind, value) from error
    else:
        return result


def _convert_boolean_value(value: object, *, nullable: bool) -> bool | None:
    if value is None or pd.isna(value):
        return None if nullable else False
    if isinstance(value, bool):
        return value
    if isinstance(value, Number):
        return value != 0

    normalized = str(value).strip().lower()
    if not normalized:
        return None if nullable else False
    return normalized != "false"


def _excel_error_as_null(
    value: object,
    *,
    enabled: bool,
    source_name: str | None,
    cell_reference: str,
) -> bool:
    if not enabled or not _is_excel_error(value):
        return False
    LOGGER.warning(
        "Excel-файл %s: ошибочное значение %r в ячейке %s заменено на NULL.",
        source_name or "<неизвестный источник>",
        value,
        cell_reference,
    )
    return True


def _is_excel_error(value: object) -> bool:
    return isinstance(value, str) and value.strip().upper() in EXCEL_ERROR_VALUES


def _preserve_text_value(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    return value if isinstance(value, str) else str(value)


def _convert_datetime_value(value: object, *, normalized: str) -> datetime:
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, int | float):
        converted = pd.Timestamp(1899, 12, 30) + pd.to_timedelta(value, unit="D")
        if isinstance(converted, pd.Timestamp):
            return converted.to_pydatetime()
        raise ValueError

    converted_value = pd.to_datetime(normalized, dayfirst=True, errors="raise")
    if isinstance(converted_value, pd.Timestamp):
        return converted_value.to_pydatetime()
    raise ValueError


def _excel_column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _raise_non_integer_value() -> None:
    raise ValueError


def _excel_engine(source_path: Path) -> Literal["pyxlsb"] | None:
    if source_path.suffix.lower() == XLSB_SUFFIX:
        return "pyxlsb"

    return None


def _is_check_column(column_name: str) -> bool:
    normalized_name = column_name.strip().lower()
    return normalized_name.startswith("check")
