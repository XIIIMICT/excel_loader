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
from sales_op.registry import SOURCE_DEFINITIONS

SPECIFICATION_FILE_NAME = "Спецификация sales_op_raw_view.xlsb"
SPECIFICATION_SHEET_NAME = "спека"
DESCRIPTION_COLUMN_INDEX = 0
TARGET_COLUMN_INDEX = 3
TYPE_COLUMN_INDEX = 4
FIRST_MAPPING_TARGET = "source_type"
LAST_MAPPING_TARGET = "comments"
REQUIRED_NOT_NULL_TARGETS = {"source_type", "doc_yearmonth", "producer_code", "seller_code"}
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
    source_columns = _find_source_columns(rows)
    sources = {
        source_code: SalesOpSourceSpecification(
            code=source_code,
            mappings=tuple(
                _build_mapping(
                    column=column,
                    description=descriptions[column.name],
                    source_expression=_cell_value(rows, row_index, source_column_index + 1),
                    row_number=row_index + 1,
                )
                for row_index, column in zip(range(first_row, last_row + 1), columns, strict=True)
            ),
        )
        for source_code, source_column_index in source_columns.items()
    }
    return SalesOpSpecification(columns=columns, descriptions=descriptions, sources=sources)


def normalize_excel_header(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value).casefold())


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
        columns.append(ColumnDefinition(raw_name, kind, raw_name not in REQUIRED_NOT_NULL_TARGETS))
        descriptions[raw_name] = str(_cell_value(rows, row_index, DESCRIPTION_COLUMN_INDEX) or raw_name)
    if len({column.name for column in columns}) != len(columns):
        message = "в спецификации повторяются имена полей БД"
        raise sales_op_error(message)
    return tuple(columns), descriptions


def _find_source_columns(rows: list[list[object]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, heading in enumerate(rows[0]):
        for definition in SOURCE_DEFINITIONS:
            if definition.matches_heading(heading):
                if definition.code in result:
                    message = f"источник {definition.code!r} указан в спецификации более одного раза"
                    raise sales_op_error(message)
                result[definition.code] = index
    missing = [definition.code for definition in SOURCE_DEFINITIONS if definition.code not in result]
    if missing:
        message = f"в спецификации не найдены источники: {', '.join(missing)}"
        raise sales_op_error(message)
    return result


def _build_mapping(
    *,
    column: ColumnDefinition,
    description: str,
    source_expression: object,
    row_number: int,
) -> SalesOpColumnMapping:
    if column.name == "doc_yearmonth":
        return SalesOpColumnMapping(column, description, "plan_date")
    normalized = normalize_excel_header(source_expression)
    if not normalized or normalized == "null":
        return SalesOpColumnMapping(column, description, "null")
    if column.name in CONSTANT_TARGETS or not isinstance(source_expression, str):
        constant = convert_excel_value(source_expression, kind=column.kind, cell_reference=f"спека!{row_number}")
        return SalesOpColumnMapping(column, description, "constant", constant=constant)
    return SalesOpColumnMapping(column, description, "source", source_name=str(source_expression))


def _cell_value(rows: list[list[object]], row_index: int, column_index: int) -> object:
    row = rows[row_index]
    return row[column_index] if column_index < len(row) else None
