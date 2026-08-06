from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd

from business_errors import SourceColumnMissingError, SourceFileNotFoundError
from dictionary_models import CombinedDictionaryPayload, WorkbookMetadata
from excel_parser import build_dictionary_payload, read_workbook_metadata

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings

TABLE_NAME = "quotes_excel_argus_pdf"
PRIMARY_KEY_COLUMNS = ("rep_date", "prod_version")
SOURCE_COLUMNS = ("rep_date", "prod_version", "min_price", "avg_price", "max_price")
REQUIRED_HEADERS = ("rep_date", "prod_version", "min_price", "avg_price", "max_price")
FILE_PATTERN = r"^Argus PDF.*\.xlsb$"
EXCEL_EPOCH = date(1899, 12, 30)


def build_payload(settings: Settings, source_paths: tuple[Path, ...]) -> CombinedDictionaryPayload:
    frames: list[pd.DataFrame] = []
    metadata: list[WorkbookMetadata] = []
    for source_path in source_paths:
        metadata.append(read_workbook_metadata(source_path, timezone=settings.app_timezone))
        frames.append(_read_source_frame(source_path))

    combined_frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=SOURCE_COLUMNS)
    payload = build_dictionary_payload(
        table_reference=settings.quotes_table_schema_reference(TABLE_NAME),
        selected_frame=combined_frame,
        primary_key_columns=PRIMARY_KEY_COLUMNS,
        not_null_columns=("avg_price",),
        column_kinds={"rep_date": "date", "min_price": "int", "avg_price": "int", "max_price": "int"},
        column_converters={"rep_date": _convert_argus_date},
        metadata=metadata[0],
        source_name=", ".join(str(path) for path in source_paths),
    )
    return CombinedDictionaryPayload(payload=payload, source_metadata=tuple(metadata))


def _read_source_frame(source_path: Path) -> pd.DataFrame:
    if not source_path.exists():
        raise SourceFileNotFoundError(source_path)
    raw_frame = pd.read_excel(
        source_path,
        sheet_name=0,
        header=None,
        keep_default_na=False,
        na_filter=False,
        engine="pyxlsb",
    )
    header_row = _find_header_row(raw_frame)
    header_values = [_normalize_header(value) for value in raw_frame.iloc[header_row]]
    column_indexes = {name: header_values.index(_normalize_header(name)) for name in REQUIRED_HEADERS}

    rows: list[dict[str, object]] = []
    for _, source_row in raw_frame.iloc[header_row + 1 :].iterrows():
        rows.append({column_name: source_row.iloc[column_indexes[column_name]] for column_name in SOURCE_COLUMNS})
    return pd.DataFrame(rows, columns=SOURCE_COLUMNS)


def _find_header_row(frame: pd.DataFrame) -> int:
    required_names = set(REQUIRED_HEADERS)
    for row_index, (_, row) in enumerate(frame.iterrows()):
        values = {_normalize_header(value) for value in row if _normalize_header(value)}
        if required_names <= values:
            return row_index
    raise SourceColumnMissingError(", ".join(REQUIRED_HEADERS))


def _normalize_header(value: object) -> str:
    return re.sub(r"\s+", "", str(value)).casefold()


def _convert_argus_date(value: object) -> date | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            return EXCEL_EPOCH + timedelta(days=int(value))
        except TypeError, ValueError, OverflowError:
            return None
    return None
