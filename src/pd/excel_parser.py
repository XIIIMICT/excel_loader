import math
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pandas as pd
from openpyxl.utils import column_index_from_string, get_column_letter

from excel_parser import load_openpyxl_workbook
from pd.models import Asset, Indicator, ParsedReport, RawMaterial

if TYPE_CHECKING:
    from openpyxl.workbook.workbook import Workbook
    from openpyxl.worksheet.cell_range import CellRange
    from openpyxl.worksheet.worksheet import Worksheet

ORDINAL_COLUMN_INDEX = column_index_from_string("D")
NAME_COLUMN_INDEX = column_index_from_string("E")
VALUE_START_COLUMN_INDEX = column_index_from_string("F")
VALUE_END_COLUMN_INDEX = column_index_from_string("AI")
REPORT_DATE_CELL = "I4"
HEADER_NAME_ROWS = (6, 7)
MAX_NAME_PARTS = 3
SECOND_NAME_PART_INDEX = 1
THIRD_NAME_PART_INDEX = 2
FOURTH_NAME_PART_INDEX = 3
FIRST_POSSIBLE_ASSET_ROW = 10
MIN_SURROUNDING_QUOTES_LENGTH = 2
DUPLICATED_NAME_PARTS_COUNT = 2
DATE_PARTS_COUNT = 3
SHORT_YEAR_LENGTH = 2
SHORT_YEAR_CENTURY_THRESHOLD = 68
LONG_YEAR_LENGTH = 4
IGNORED_RAW_MATERIAL_ROW_NAMES = frozenset({"Орехо-Зуево", "Орехово-Зуево", "Губаха"})
ASSET_NAME_WITH_DATE_RE = re.compile(r'^"(?P<name>.+)"\s+\(на\s+\d{2}\.\d{2}\.\d{2,4}\)$', re.IGNORECASE)

EXCEL_ERROR_VALUES = {
    "#DIV/0!",
    "#N/A",
    "#NAME?",
    "#NULL!",
    "#NUM!",
    "#REF!",
    "#VALUE!",
    "#SPILL!",
    "#CALC!",
    "#FIELD!",
    "#GETTING_DATA",
}

type NormalizedCellValue = str | int | float | bool | Decimal | date | datetime
type MergedValueMap = dict[tuple[int, int], object]


def parse_report(source_path: str | Path, *, loaded_at: datetime) -> ParsedReport:
    path = Path(source_path)
    if not path.exists():
        msg = f"Файл не найден: {path}"
        raise FileNotFoundError(msg)

    formula_workbook = load_openpyxl_workbook(path, data_only=False)
    data_workbook = load_openpyxl_workbook(path, data_only=True)
    try:
        formula_sheet = _select_first_sheet(formula_workbook)
        data_sheet = _select_first_sheet(data_workbook)

        assets, raw_materials = _parse_assets_and_raw_materials(
            formula_sheet=formula_sheet,
            data_sheet=data_sheet,
        )
        indicators = _parse_report_indicators(
            formula_sheet,
            data_sheet=data_sheet,
            raw_materials=raw_materials,
        )
        data = _parse_data_frame(
            data_sheet=data_sheet,
            raw_materials=raw_materials,
            indicators=indicators,
            loaded_at=loaded_at,
        )
        return ParsedReport(
            source_name=path.name,
            report_date=_parse_report_date(data_sheet[REPORT_DATE_CELL].value),
            loaded_at=loaded_at,
            assets=tuple(assets),
            raw_materials=tuple(raw_materials),
            indicators=tuple(indicators),
            data=data,
        )
    finally:
        formula_workbook.close()
        data_workbook.close()


def export_reference_csv(report: ParsedReport, output_dir: str | Path) -> None:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    report.assets_frame.to_csv(output_path / "assets.csv", index=False, encoding="utf-8-sig")
    report.raw_materials_frame.to_csv(output_path / "raw_materials.csv", index=False, encoding="utf-8-sig")
    report.indicators_frame.to_csv(output_path / "indicators.csv", index=False, encoding="utf-8-sig")


def _select_first_sheet(workbook: Workbook) -> Worksheet:
    if not workbook.worksheets:
        msg = "В файле не найдено ни одного листа."
        raise ValueError(msg)

    return workbook.worksheets[0]


def _parse_report_indicators(
    sheet: Worksheet,
    *,
    data_sheet: Worksheet,
    raw_materials: list[RawMaterial],
) -> list[Indicator]:
    merged_values = _build_merged_value_map(sheet)
    indicators: list[Indicator] = []
    # The PD parser follows the approved current summary layout. Older 2025
    # workbooks may contain a long legacy tail after this range; those columns
    # are intentionally ignored instead of being auto-promoted to indicators.
    last_column_index = min(VALUE_END_COLUMN_INDEX, sheet.max_column, data_sheet.max_column)
    for column_index in range(VALUE_START_COLUMN_INDEX, last_column_index + 1):
        parts = _column_name_parts(sheet, merged_values, column_index)
        if not parts:
            if not _column_has_any_data(data_sheet, raw_materials, column_index):
                continue
            letter = get_column_letter(column_index)
            msg = f"Не удалось определить имя показателя для колонки {letter}."
            raise ValueError(msg)

        padded_parts = (*parts, *(None for _ in range(MAX_NAME_PARTS - len(parts))))
        indicators.append(
            Indicator(
                xls_col=get_column_letter(column_index),
                xls_col_index=column_index,
                ordinal_number=len(indicators) + 1,
                category_1=padded_parts[0] if len(parts) > SECOND_NAME_PART_INDEX else None,
                category_2=padded_parts[1] if len(parts) > THIRD_NAME_PART_INDEX else None,
                category_3=padded_parts[2] if len(parts) > FOURTH_NAME_PART_INDEX else None,
                name=parts[-1],
                db_name=None,
            ),
        )

    duplicates = sorted(
        names for names, count in Counter(indicator.full_name for indicator in indicators).items() if count > 1
    )
    if duplicates:
        duplicate_text = "; ".join(duplicates)
        msg = f"После нормализации найдены неуникальные имена колонок: {duplicate_text}."
        raise ValueError(msg)

    return indicators


def _column_name_parts(sheet: Worksheet, merged_values: MergedValueMap, column_index: int) -> tuple[str, ...]:
    parts = [_header_part(sheet, merged_values, row, column_index) for row in HEADER_NAME_ROWS]

    if parts[0] is None and parts[1] is not None and column_index < VALUE_END_COLUMN_INDEX:
        parts[0] = _header_part(sheet, merged_values, HEADER_NAME_ROWS[0], column_index + 1)

    normalized_parts = [part for part in parts if part]
    if len(normalized_parts) == DUPLICATED_NAME_PARTS_COUNT and normalized_parts[0] == normalized_parts[1]:
        normalized_parts = [normalized_parts[0]]
    if len(normalized_parts) > MAX_NAME_PARTS:
        letter = get_column_letter(column_index)
        msg = f"Для колонки {letter} найдено больше {MAX_NAME_PARTS} уровней имени показателя."
        raise ValueError(msg)

    return tuple(normalized_parts)


def _parse_assets_and_raw_materials(
    *,
    formula_sheet: Worksheet,
    data_sheet: Worksheet,
) -> tuple[list[Asset], list[RawMaterial]]:
    assets: list[Asset] = []
    raw_materials: list[RawMaterial] = []
    current_asset_name: str | None = None
    hierarchy_stack: dict[int, tuple[str, ...]] = {}

    for row_number in range(FIRST_POSSIBLE_ASSET_ROW, data_sheet.max_row + 1):
        raw_name = data_sheet.cell(row_number, NAME_COLUMN_INDEX).value
        if _is_blank(raw_name):
            continue

        ordinal_value = normalize_cell_value(data_sheet.cell(row_number, ORDINAL_COLUMN_INDEX).value)
        if _is_asset_row(formula_sheet=formula_sheet, data_sheet=data_sheet, row_number=row_number):
            current_asset_name = clean_asset_name(raw_name)
            assets.append(
                Asset(
                    name=current_asset_name,
                    xls_row=row_number,
                ),
            )
            hierarchy_stack = {}
            continue

        if current_asset_name is None:
            continue

        if ordinal_value is None and not _row_has_any_data(data_sheet, row_number):
            continue

        source_name = str(raw_name)
        clean_name = clean_display_name(raw_name)
        if clean_name in IGNORED_RAW_MATERIAL_ROW_NAMES:
            continue

        hierarchy_level = len(source_name) - len(source_name.lstrip(" "))
        parent_path = _find_parent_path(hierarchy_stack, hierarchy_level)
        ordinal_number = len(raw_materials) + 1
        material_path = (*parent_path, clean_name)
        raw_materials.append(
            RawMaterial(
                asset_name=current_asset_name,
                ordinal_number=ordinal_number,
                name=clean_name,
                parent_name=parent_path[-1] if parent_path else None,
                material_path=material_path,
                xls_row=row_number,
            ),
        )
        _update_hierarchy_stack(hierarchy_stack, hierarchy_level, material_path)

    if not assets:
        msg = "Не удалось найти активы. Проверьте правило: актив = жирная строка в E без номера в D."
        raise ValueError(msg)
    if not raw_materials:
        msg = "Не удалось найти строки сырья/номенклатуры."
        raise ValueError(msg)

    return assets, raw_materials


def _parse_data_frame(
    *,
    data_sheet: Worksheet,
    raw_materials: list[RawMaterial],
    indicators: list[Indicator],
    loaded_at: datetime,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for raw_material in raw_materials:
        if raw_material.xls_row is None:
            continue
        for indicator in indicators:
            if indicator.xls_col_index is None:
                continue
            value = normalize_cell_value(data_sheet.cell(raw_material.xls_row, indicator.xls_col_index).value)
            rows.append(
                {
                    "asset_name": raw_material.asset_name,
                    "raw_material_name": raw_material.name,
                    "material_path": raw_material.material_path,
                    "ordinal_number": raw_material.ordinal_number,
                    "indicator_name": indicator.full_name,
                    "value": value_to_text(value),
                    "loaded_at": loaded_at,
                },
            )
    return pd.DataFrame(rows)


def clean_display_name(value: object) -> str:
    text = _collapse_ws(value)
    if len(text) >= MIN_SURROUNDING_QUOTES_LENGTH and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    if len(text) >= MIN_SURROUNDING_QUOTES_LENGTH and text[0] == "«" and text[-1] == "»":
        text = text[1:-1].strip()
    return text


def clean_asset_name(value: object) -> str:
    text = clean_display_name(value)
    match = ASSET_NAME_WITH_DATE_RE.fullmatch(text)
    if match is None:
        return text
    return clean_display_name(match.group("name"))


def normalize_cell_value(value: object) -> NormalizedCellValue | None:
    normalized_value: NormalizedCellValue | None
    if value is None or (isinstance(value, float) and math.isnan(value)):
        normalized_value = None
    elif isinstance(value, str):
        stripped = _collapse_ws(value)
        if (
            (stripped in {"", "-"})
            or (stripped in EXCEL_ERROR_VALUES)
            or (stripped.startswith("#") and stripped.endswith(("!", "?")))
        ):
            normalized_value = None
        else:
            normalized_value = stripped
    elif isinstance(value, bool | int | float | Decimal | datetime | date):
        normalized_value = value
    else:
        normalized_value = _collapse_ws(value) or None
    return normalized_value


def split_value_for_storage(value: object | None) -> tuple[Decimal | None, str | None]:
    result: tuple[Decimal | None, str | None]
    if value is None:
        result = (None, None)
    elif isinstance(value, bool):
        result = (None, "TRUE" if value else "FALSE")
    elif isinstance(value, int | Decimal):
        result = (Decimal(value), None)
    elif isinstance(value, float):
        result = _split_float_value(value)
    elif isinstance(value, datetime):
        result = (None, value.date().isoformat())
    elif isinstance(value, date):
        result = (None, value.isoformat())
    elif isinstance(value, str):
        result = _split_string_value(value)
    else:
        result = (None, str(value))
    return result


def value_to_text(value: object | None) -> str | None:
    _, text_value = split_value_for_storage(value)
    if text_value is not None:
        return text_value
    if value is None:
        return None
    return str(value)


def _parse_report_date(value: object) -> date:
    normalized_value = normalize_cell_value(value)
    if isinstance(normalized_value, datetime):
        return normalized_value.date()
    if isinstance(normalized_value, date):
        return normalized_value
    if isinstance(normalized_value, str):
        parsed_date = _parse_report_date_text(normalized_value)
        if parsed_date is not None:
            return parsed_date

    msg = f"Не удалось прочитать дату отчета из ячейки {REPORT_DATE_CELL}: {value!r}."
    raise ValueError(msg)


def _is_asset_row(*, formula_sheet: Worksheet, data_sheet: Worksheet, row_number: int) -> bool:
    ordinal = normalize_cell_value(data_sheet.cell(row_number, ORDINAL_COLUMN_INDEX).value)
    name = data_sheet.cell(row_number, NAME_COLUMN_INDEX).value
    return ordinal is None and not _is_blank(name) and bool(formula_sheet.cell(row_number, NAME_COLUMN_INDEX).font.bold)


def _row_has_any_data(data_sheet: Worksheet, row_number: int) -> bool:
    return any(
        normalize_cell_value(data_sheet.cell(row_number, column_index).value) is not None
        for column_index in range(VALUE_START_COLUMN_INDEX, VALUE_END_COLUMN_INDEX + 1)
    )


def _find_parent_path(hierarchy_stack: dict[int, tuple[str, ...]], hierarchy_level: int) -> tuple[str, ...]:
    lower_levels = [level for level in hierarchy_stack if level < hierarchy_level]
    if not lower_levels:
        return ()
    return hierarchy_stack[max(lower_levels)]


def _update_hierarchy_stack(
    hierarchy_stack: dict[int, tuple[str, ...]], hierarchy_level: int, material_path: tuple[str, ...]
) -> None:
    for existing_level in list(hierarchy_stack):
        if existing_level >= hierarchy_level:
            del hierarchy_stack[existing_level]
    hierarchy_stack[hierarchy_level] = material_path


def _column_has_any_data(data_sheet: Worksheet, raw_materials: list[RawMaterial], column_index: int) -> bool:
    return any(
        raw_material.xls_row is not None
        and normalize_cell_value(data_sheet.cell(raw_material.xls_row, column_index).value) is not None
        for raw_material in raw_materials
    )


def _build_merged_value_map(sheet: Worksheet) -> MergedValueMap:
    values: MergedValueMap = {}
    for merged_range in _merged_ranges(sheet):
        min_row = cast("int", merged_range.min_row)
        min_col = cast("int", merged_range.min_col)
        max_row = cast("int", merged_range.max_row)
        max_col = cast("int", merged_range.max_col)
        top_left_value = sheet.cell(min_row, min_col).value
        for row in range(min_row, max_row + 1):
            for column in range(min_col, max_col + 1):
                values[(row, column)] = top_left_value
    return values


def _split_float_value(value: float) -> tuple[Decimal | None, str | None]:
    if math.isnan(value) or math.isinf(value):
        return None, None
    return Decimal(str(value)), None


def _split_string_value(value: str) -> tuple[Decimal | None, str | None]:
    try:
        return Decimal(value.replace(" ", "").replace(",", ".")), None
    except InvalidOperation, ValueError:
        return None, value


def _parse_report_date_text(value: str) -> date | None:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)

    dot_parts = value.split(".")
    if len(dot_parts) != DATE_PARTS_COUNT:
        return None

    day_text, month_text, year_text = dot_parts
    if not day_text.isdigit() or not month_text.isdigit() or not year_text.isdigit():
        return None

    year = int(year_text)
    if len(year_text) == SHORT_YEAR_LENGTH:
        year += 2000 if year <= SHORT_YEAR_CENTURY_THRESHOLD else 1900
    elif len(year_text) != LONG_YEAR_LENGTH:
        return None

    try:
        return date(year, int(month_text), int(day_text))
    except ValueError:
        return None


def _merged_ranges(sheet: Worksheet) -> tuple[CellRange, ...]:
    merged_ranges = cast("object", sheet.merged_cells.ranges)
    if isinstance(merged_ranges, set):
        return tuple(cast("set[CellRange]", merged_ranges))
    return tuple(cast("list[CellRange]", merged_ranges))


def _header_part(sheet: Worksheet, merged_values: MergedValueMap, row: int, column: int) -> str | None:
    value = merged_values.get((row, column), sheet.cell(row, column).value)
    if isinstance(value, str) and value.startswith("="):
        return None
    normalized = _collapse_ws(value)
    if not normalized:
        return None
    return normalized


def _collapse_ws(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.;:%])", r"\1", text)


def _is_blank(value: object) -> bool:
    return _collapse_ws(value) == ""
