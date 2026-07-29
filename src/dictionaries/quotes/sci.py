from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pandas as pd

from business_errors import CellTypeConversionError, SourceColumnMissingError
from dictionary_models import CombinedDictionaryPayload, WorkbookMetadata
from excel_parser import build_dictionary_payload, convert_excel_value, read_workbook_metadata

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings

TABLE_NAME = "quotes_excel_sci"
PRIMARY_KEY_COLUMNS = ("pub_date", "prod_version", "product_id")
TARGET_COLUMNS = ("pub_date", "prod_version", "avg_price", "product_id")
HEADER_NAMES = ("Дата", "Товар", "Марка", "CIF China, $/т")
MARKET_SUFFIX_PATTERN = re.compile(r"\s+market\s*$", re.IGNORECASE)
SHEET_PATTERNS = {
    "ски": (
        re.compile(r"zeon", re.IGNORECASE),
        re.compile(r"^ски-3 циндао$", re.IGNORECASE),
        re.compile(r"^ски-3с циндао$", re.IGNORECASE),
    ),
    "скс": (re.compile(r"east china", re.IGNORECASE),),
}


def build_payload(settings: Settings, source_paths: tuple[Path, ...]) -> CombinedDictionaryPayload:
    frames: list[pd.DataFrame] = []
    metadata: list[WorkbookMetadata] = []
    for source_path in source_paths:
        metadata.append(read_workbook_metadata(source_path, timezone=settings.app_timezone))
        frames.extend(_read_source_frames(source_path))

    combined_frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=TARGET_COLUMNS)
    payload = build_dictionary_payload(
        table_reference=settings.quotes_table_schema_reference(TABLE_NAME),
        selected_frame=combined_frame,
        primary_key_columns=PRIMARY_KEY_COLUMNS,
        not_null_columns=("pub_date", "prod_version", "avg_price", "product_id"),
        column_kinds={"pub_date": "date", "avg_price": "float"},
        metadata=metadata[0],
        source_name=", ".join(str(path) for path in source_paths),
    )
    return CombinedDictionaryPayload(payload=payload, source_metadata=tuple(metadata))


def _read_source_frames(source_path: Path) -> list[pd.DataFrame]:
    workbook = pd.ExcelFile(source_path)
    patterns = _sheet_patterns(source_path)
    selected_sheets = [
        sheet_name
        for sheet_name in workbook.sheet_names
        if any(pattern.search(_normalize_text(sheet_name)) for pattern in patterns)
    ]
    if len(selected_sheets) != len(patterns):
        missing_name = "ожидаемые листы SCI"
        raise SourceColumnMissingError(missing_name)

    return [_read_sheet_frame(source_path, sheet_name) for sheet_name in selected_sheets]


def _read_sheet_frame(source_path: Path, sheet_name: str) -> pd.DataFrame:
    raw_frame = pd.read_excel(source_path, sheet_name=sheet_name, header=None, keep_default_na=False, na_filter=False)
    header_row = _find_header_row(raw_frame)
    header_values = [_normalize_text(value) for value in raw_frame.iloc[header_row]]
    column_indexes = {name: header_values.index(_normalize_text(name)) for name in HEADER_NAMES}
    product_id = raw_frame.iloc[2, 3]

    rows: list[dict[str, object]] = []
    for _, source_row in raw_frame.iloc[header_row + 1 :].iterrows():
        pub_date = source_row.iloc[column_indexes["Дата"]]
        product = _clean_component(source_row.iloc[column_indexes["Товар"]])
        brand = _clean_component(source_row.iloc[column_indexes["Марка"]])
        if not _is_date_value(pub_date) or not product or not brand:
            continue
        product_version = MARKET_SUFFIX_PATTERN.sub("", f"{product} {brand}").strip()
        rows.append(
            {
                "pub_date": pub_date,
                "prod_version": f"{product_version} CIF China, USD/т",
                "avg_price": source_row.iloc[column_indexes["CIF China, $/т"]],
                "product_id": product_id,
            },
        )
    return pd.DataFrame(rows, columns=TARGET_COLUMNS)


def _find_header_row(frame: pd.DataFrame) -> int:
    required_names = {_normalize_text(name) for name in HEADER_NAMES}
    for row_index, (_, row) in enumerate(frame.iterrows()):
        values = {_normalize_text(value) for value in row if _normalize_text(value)}
        if required_names <= values:
            return row_index
    raise SourceColumnMissingError(", ".join(HEADER_NAMES))


def _sheet_patterns(source_path: Path) -> tuple[re.Pattern[str], ...]:
    normalized_name = source_path.name.casefold()
    for marker, patterns in SHEET_PATTERNS.items():
        if marker in normalized_name:
            return patterns
    msg = f"Не удалось определить тип SCI-файла {source_path}."
    raise ValueError(msg)


def _normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _clean_component(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return MARKET_SUFFIX_PATTERN.sub("", _normalize_text(value)).strip()


def _is_date_value(value: object) -> bool:
    try:
        converted = convert_excel_value(value, kind="date", cell_reference="Дата")
    except CellTypeConversionError:
        return False
    return converted is not None
