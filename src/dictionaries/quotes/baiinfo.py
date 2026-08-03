from __future__ import annotations

import re
from datetime import date, datetime
from typing import TYPE_CHECKING

import pandas as pd

from business_errors import SourceColumnMissingError, SourceFileNotFoundError
from dictionary_models import CombinedDictionaryPayload, WorkbookMetadata
from excel_parser import build_dictionary_payload, read_workbook_metadata

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings

TABLE_NAME = "quotes_excel_baiinfo"
PRIMARY_KEY_COLUMNS = ("pub_date", "prod_version")
SOURCE_COLUMNS = ("Date", "prod_version", "Latest Price", "Unit")
SOURCE_COLUMN_ALIASES = {
    "Date": "pub_date",
    "Latest Price": "avg_price",
}
DROPPED_COLUMNS = ("Unit",)
WHERE_COLUMNS: dict[str, object] = {"Unit": "USD/T"}
REQUIRED_HEADERS = ("date", "latestprice", "unit")
FILE_PATTERN = r"^Baiinfo\. (?P<product>Меламин|Метанол|Параформальдегид|Пентаэритритол|Уротропин)\.xlsb$"
BAIINFO_DATE_FORMAT = "%d-%b-%y"
PRODUCT_PROD_VERSIONS = {
    "Меламин": "Melamine Average Market Price (Domestic China)",
    "Метанол": "Methanol Average Market Price (Domestic China)",
    "Параформальдегид": "Paraformaldehyde Average Market Price (Domestic China)",
    "Пентаэритритол": "Pentaerythritol Average Market Price (Domestic China)",
    "Уротропин": "Urotropine Average Market Price (Domestic China)",
}


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
        column_kinds={"pub_date": "date", "avg_price": "float"},
        where_columns=WHERE_COLUMNS,
        dropped_columns=DROPPED_COLUMNS,
        column_aliases=SOURCE_COLUMN_ALIASES,
        column_converters={"pub_date": _convert_baiinfo_date},
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
    product_version = _prod_version_for(source_path.name)

    rows: list[dict[str, object]] = []
    for _, source_row in raw_frame.iloc[header_row + 1 :].iterrows():
        rows.append(
            {
                "Date": source_row.iloc[column_indexes["date"]],
                "prod_version": product_version,
                "Latest Price": source_row.iloc[column_indexes["latestprice"]],
                "Unit": source_row.iloc[column_indexes["unit"]],
            },
        )
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


def _prod_version_for(file_name: str) -> str:
    for product_name, prod_version in PRODUCT_PROD_VERSIONS.items():
        if product_name in file_name:
            return prod_version
    msg = f"Не удалось определить продукт по имени файла Baiinfo: {file_name!r}."
    raise ValueError(msg)


def _convert_baiinfo_date(value: object) -> date | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.strptime(str(value).strip(), BAIINFO_DATE_FORMAT)
    except ValueError:
        return None
