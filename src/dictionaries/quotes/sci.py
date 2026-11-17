import re
from typing import TYPE_CHECKING

import pandas as pd

from business_errors import SourceColumnMissingError
from dictionary_models import CombinedDictionaryPayload, WorkbookMetadata
from excel_parser import (
    EXCEL_ENGINE,
    build_dictionary_payload,
    excel_column_name,
    read_workbook_metadata,
)

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings

TABLE_NAME = "quotes_excel_sci"
PRIMARY_KEY_COLUMNS = ("pub_date", "prod_version", "product_id")
TARGET_COLUMNS = ("pub_date", "prod_version", "avg_price", "product_id")
SOURCE_COLUMNS = ("Дата", "prod_version", "CIF China, $/т", "product_id")
SOURCE_COLUMN_ALIASES = {
    "Дата": "pub_date",
    "CIF China, $/т": "avg_price",
}
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
    locations: list[dict[str, str]] = []
    metadata: list[WorkbookMetadata] = []
    for source_path in source_paths:
        metadata.append(read_workbook_metadata(source_path, timezone=settings.app_timezone))
        source_frames, source_locations = _read_source_frames(source_path)
        frames.extend(source_frames)
        locations.extend(source_locations)

    combined_frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=SOURCE_COLUMNS)
    payload = build_dictionary_payload(
        table_reference=settings.modules["quotes"].table_reference(TABLE_NAME),
        selected_frame=combined_frame,
        primary_key_columns=PRIMARY_KEY_COLUMNS,
        not_null_columns=("pub_date", "prod_version", "avg_price", "product_id"),
        column_kinds={"pub_date": "date", "avg_price": "float"},
        metadata=metadata[0],
        source_name=", ".join(str(path) for path in source_paths),
        column_aliases=SOURCE_COLUMN_ALIASES,
        cell_locations=locations,
    )
    return CombinedDictionaryPayload(payload=payload, source_metadata=tuple(metadata))


def _read_source_frames(source_path: Path) -> tuple[list[pd.DataFrame], list[dict[str, str]]]:
    patterns = _sheet_patterns(source_path)
    frames: list[pd.DataFrame] = []
    locations: list[dict[str, str]] = []
    with pd.ExcelFile(source_path, engine=EXCEL_ENGINE) as workbook:
        selected_sheets = [
            sheet_name
            for sheet_name in workbook.sheet_names
            if any(pattern.search(_normalize_text(sheet_name)) for pattern in patterns)
        ]
        if len(selected_sheets) != len(patterns):
            missing_name = "ожидаемые листы SCI"
            raise SourceColumnMissingError(missing_name)

        for sheet_name in selected_sheets:
            frame, frame_locations = _read_sheet_frame(workbook, sheet_name)
            frames.append(frame)
            locations.extend(frame_locations)
    return frames, locations


def _read_sheet_frame(workbook: pd.ExcelFile, sheet_name: str) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    raw_frame = pd.read_excel(workbook, sheet_name=sheet_name, header=None, keep_default_na=False, na_filter=False)
    header_row = _find_header_row(raw_frame)
    header_values = [_normalize_text(value) for value in raw_frame.iloc[header_row]]
    column_indexes = {name: header_values.index(_normalize_text(name)) for name in HEADER_NAMES}
    product_id = raw_frame.iloc[2, 3]
    date_column = column_indexes["Дата"]
    price_column = column_indexes["CIF China, $/т"]
    date_letter = excel_column_name(date_column + 1)
    price_letter = excel_column_name(price_column + 1)

    rows: list[dict[str, object]] = []
    locations: list[dict[str, str]] = []
    for row_position, row in enumerate(raw_frame.iloc[header_row + 1 :].itertuples(index=False, name=None)):
        if all(_is_blank_cell(value) for value in row):
            # A fully empty row ends the data block: everything after it
            # (repeated data, totals) is not part of the report.
            break
        product = _clean_component(row[column_indexes["Товар"]])
        if not product:
            # Monthly/aggregate rows keep only calculated prices (and a month
            # name instead of a date). They are not quotes: skip, keep reading.
            continue
        brand = _clean_component(row[column_indexes["Марка"]])
        product_version = MARKET_SUFFIX_PATTERN.sub("", f"{product} {brand}").strip()
        rows.append(
            {
                "Дата": row[date_column],
                "prod_version": f"{product_version} CIF China, USD/т",
                "CIF China, $/т": row[price_column],
                "product_id": product_id,
            },
        )
        excel_row = header_row + row_position + 2
        locations.append(
            {
                "Дата": f"{sheet_name}!{date_letter}{excel_row}",
                "CIF China, $/т": f"{sheet_name}!{price_letter}{excel_row}",
            },
        )
    return pd.DataFrame(rows, columns=SOURCE_COLUMNS), locations


def _find_header_row(frame: pd.DataFrame) -> int:
    required_names = {_normalize_text(name) for name in HEADER_NAMES}
    normalized_rows = (
        {normalized for normalized in map(_normalize_text, row) if normalized}
        for row in frame.itertuples(index=False, name=None)
    )
    for row_index, values in enumerate(normalized_rows):
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


def _is_blank_cell(value: object) -> bool:
    if value is None or pd.isna(value):
        return True
    return _normalize_text(value) == ""
