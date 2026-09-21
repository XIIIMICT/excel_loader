import re
from typing import TYPE_CHECKING

from pyxlsb import open_workbook

from dictionary_models import ColumnDefinition, ColumnKind
from excel_parser import convert_excel_value
from sales_op.models import (
    SalesOpColumnMapping,
    SalesOpSourceSpecification,
    SalesOpSpecification,
    sales_op_error,
)

SPECIFICATION_FILE_NAME = "Спецификация sales_op_raw_view.xlsb"
SPECIFICATION_SHEET_NAME = "спека"
DESCRIPTION_COLUMN_INDEX = 0
TARGET_COLUMN_INDEX = 3
TYPE_COLUMN_INDEX = 4
NOT_NULLABLE_COLUMN_INDEX = 5
FIRST_MAPPING_TARGET = "source_type"
LAST_MAPPING_TARGET = "comments"
FIRST_SOURCE_COLUMN_INDEX = 7
SOURCE_COLUMN_STEP = 2
SOURCE_NAME_ROW = 0
SOURCE_TABLE_ROW = 8
SOURCE_FILE_MASK_ROW = 7
TABLE_SCHEMA_PREFIX = "el."
TABLE_NAME_PREFIX = "sales_op_"
MULTIPLE_BACKSLASHES_PATTERN = re.compile(r"\\{2,}")
VALID_TARGET_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")
TYPE_MAPPING: dict[str, ColumnKind] = {
    "date": "date",
    "datetime": "datetime",
    "float": "float",
    "hash": "text",
    "int": "int",
    "integer": "int",
    "text": "text",
}
CONSTANT_TARGETS = {"source_type", "uom_code"}
TRUTHY_MARKERS = {"y", "yes", "да", "true", "1", "+"}

if TYPE_CHECKING:
    from pathlib import Path

    from pyxlsb.workbook import Workbook


def read_sales_op_specification(source_path: Path) -> SalesOpSpecification:
    if not source_path.is_file():
        message = f"файл спецификации не найден: {source_path}"
        raise sales_op_error(message)
    try:
        with open_workbook(source_path) as workbook:
            rows = _read_sheet_rows(workbook, SPECIFICATION_SHEET_NAME)
    except Exception as error:
        message = f"не удалось прочитать спецификацию {source_path}: {error}"
        raise sales_op_error(message) from error

    first_row, last_row = _mapping_row_bounds(rows)
    columns, descriptions = _read_columns(rows, first_row, last_row)
    sources = {
        source_code: SalesOpSourceSpecification(
            code=source_code,
            file_pattern=file_pattern,
            mappings=tuple(
                _build_mapping(
                    column=column,
                    description=descriptions[column.name],
                    source_expression=_cell_value(rows, row_index, source_column_index + 1),
                    row_number=row_index + 1,
                    required=not column.nullable,
                )
                for row_index, column in zip(range(first_row, last_row + 1), columns, strict=True)
            ),
        )
        for source_code, source_column_index, file_pattern in _read_sources(rows)
    }
    return SalesOpSpecification(columns=columns, descriptions=descriptions, sources=sources)


def normalize_excel_header(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value).casefold())


def source_table_name(source_code: str) -> str:
    return f"{TABLE_NAME_PREFIX}{source_code}"


def _read_sheet_rows(workbook: Workbook, sheet_name: str) -> list[list[object]]:
    if sheet_name not in workbook.sheets:
        message = f"в спецификации отсутствует лист {sheet_name!r}"
        raise sales_op_error(message)
    with workbook.get_sheet(sheet_name) as sheet:
        return [[cell.v for cell in row] for row in sheet.rows()]


def _mapping_row_bounds(rows: list[list[object]]) -> tuple[int, int]:
    targets = [_cell_value(rows, index, TARGET_COLUMN_INDEX) for index in range(len(rows))]
    try:
        first_row = targets.index(FIRST_MAPPING_TARGET)
        last_row = targets.index(LAST_MAPPING_TARGET, first_row)
    except ValueError as error:
        message = f"не найден диапазон {FIRST_MAPPING_TARGET!r} ... {LAST_MAPPING_TARGET!r}"
        raise sales_op_error(message) from error
    return first_row, last_row


def _read_sources(rows: list[list[object]]) -> list[tuple[str, int, str]]:
    sources: list[tuple[str, int, str]] = []
    seen_codes: set[str] = set()
    for column_index in range(FIRST_SOURCE_COLUMN_INDEX, _row_width(rows), SOURCE_COLUMN_STEP):
        if not isinstance(_cell_value(rows, SOURCE_NAME_ROW, column_index), str):
            continue
        table_reference = _cell_value(rows, SOURCE_TABLE_ROW, column_index)
        if not isinstance(table_reference, str) or not table_reference.strip():
            continue
        source_code = _source_code(table_reference)
        if source_code in seen_codes:
            message = f"источник {source_code!r} указан в спецификации более одного раза"
            raise sales_op_error(message)
        seen_codes.add(source_code)
        sources.append((source_code, column_index, _file_pattern(rows, column_index, source_code=source_code)))
    if not sources:
        message = "в спецификации не найдены источники с таблицей КХД"
        raise sales_op_error(message)
    return sources


def _source_code(table_reference: str) -> str:
    table_name = table_reference.strip()
    table_name = table_name.removeprefix(TABLE_SCHEMA_PREFIX)
    table_name = table_name.removeprefix(TABLE_NAME_PREFIX)
    if not VALID_TARGET_NAME_PATTERN.fullmatch(table_name):
        message = f"в спецификации указана некорректная таблица КХД: {table_reference!r}"
        raise sales_op_error(message)
    return table_name


def _file_pattern(rows: list[list[object]], column_index: int, *, source_code: str) -> str:
    raw_pattern = _cell_value(rows, SOURCE_FILE_MASK_ROW, column_index)
    if not isinstance(raw_pattern, str) or not raw_pattern.strip():
        message = f"для источника {source_code!r} не задана маска файла (регулярное выражение)"
        raise sales_op_error(message)
    # The specification stores escaped backslashes ("\\."); collapse them so the
    # mask matches a literal dot instead of a backslash.
    pattern = MULTIPLE_BACKSLASHES_PATTERN.sub(lambda _match: "\\", raw_pattern.strip())
    try:
        re.compile(pattern)
    except re.error as error:
        message = f"для источника {source_code!r} указана некорректная маска файла {raw_pattern!r}: {error}"
        raise sales_op_error(message) from error
    return pattern


def _row_width(rows: list[list[object]]) -> int:
    return max((len(row) for row in rows), default=0)


def _read_columns(
    rows: list[list[object]],
    first_row: int,
    last_row: int,
) -> tuple[tuple[ColumnDefinition, ...], dict[str, str]]:
    columns: list[ColumnDefinition] = []
    descriptions: dict[str, str] = {}
    for row_index in range(first_row, last_row + 1):
        raw_name = _cell_value(rows, row_index, TARGET_COLUMN_INDEX)
        if not isinstance(raw_name, str) or not VALID_TARGET_NAME_PATTERN.fullmatch(raw_name):
            message = f"в строке {row_index + 1} указано некорректное имя поля БД: {raw_name!r}"
            raise sales_op_error(message)
        raw_kind = normalize_excel_header(_cell_value(rows, row_index, TYPE_COLUMN_INDEX))
        kind = TYPE_MAPPING.get(raw_kind)
        if kind is None:
            message = f"в строке {row_index + 1} указан неизвестный тип данных: {raw_kind!r}"
            raise sales_op_error(message)
        required = _is_not_nullable(_cell_value(rows, row_index, NOT_NULLABLE_COLUMN_INDEX))
        columns.append(ColumnDefinition(raw_name, kind, nullable=not required))
        descriptions[raw_name] = str(_cell_value(rows, row_index, DESCRIPTION_COLUMN_INDEX) or raw_name)
    if len({column.name for column in columns}) != len(columns):
        message = "в спецификации повторяются имена полей БД"
        raise sales_op_error(message)
    return tuple(columns), descriptions


def _is_not_nullable(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return value is True
    normalized = str(value).strip().casefold()
    return normalized in TRUTHY_MARKERS


def _build_mapping(
    *,
    column: ColumnDefinition,
    description: str,
    source_expression: object,
    row_number: int,
    required: bool,
) -> SalesOpColumnMapping:
    if column.name == "doc_yearmonth":
        return SalesOpColumnMapping(column, description, "plan_date", required=required)
    normalized = normalize_excel_header(source_expression)
    if not normalized or normalized == "null":
        return SalesOpColumnMapping(column, description, "null", required=required)
    if column.name in CONSTANT_TARGETS or not isinstance(source_expression, str):
        constant = convert_excel_value(source_expression, kind=column.kind, cell_reference=f"спека!{row_number}")
        return SalesOpColumnMapping(column, description, "constant", constant=constant, required=required)
    return SalesOpColumnMapping(column, description, "source", source_name=str(source_expression), required=required)


def _cell_value(rows: list[list[object]], row_index: int, column_index: int) -> object:
    if row_index >= len(rows):
        return None
    row = rows[row_index]
    return row[column_index] if column_index < len(row) else None
