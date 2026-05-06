from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING
from warnings import catch_warnings, filterwarnings, simplefilter

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import from_excel

from app_logging import get_logger
from business_errors import CellTypeConversionError
from excel_parser import EXCEL_ERROR_VALUES, convert_excel_value
from sales_op.models import (
    ParsedSalesOpFile,
    SalesOpColumnMapping,
    SalesOpSourceSpecification,
    sales_op_error,
)
from sales_op.specification import normalize_excel_header

filterwarnings(
    "ignore",
    message="Unknown extension is not supported and will be removed",
    category=UserWarning,
    module=r"openpyxl\.worksheet\._reader",
)

if TYPE_CHECKING:
    from pathlib import Path

    from openpyxl.worksheet.worksheet import Worksheet

    from dictionary_models import DbRow

PLAN_MARKER = "планпродаж"
PRODUCER_HEADER = "производитель"
COMMENTS_HEADER = "комментарии"
PRODUCER_TARGET = "producer_code"
SELLER_TARGET = "seller_code"
LOGGER = get_logger("sales_op")


def inspect_sales_op_workbook(source_path: Path) -> tuple[date, datetime]:
    with _workbook_warnings_suppressed():
        workbook = _load_workbook(source_path)
    try:
        worksheet = _first_visible_sheet(workbook)
        marker_row, marker_column = _find_plan_marker(worksheet)
        plan_date = _parse_plan_date(worksheet.cell(marker_row, marker_column + 1).value)
        modified_at = workbook.properties.modified or datetime.fromtimestamp(source_path.stat().st_mtime)  # noqa: DTZ006
        return plan_date, modified_at
    finally:
        workbook.close()


def parse_sales_op_workbook(
    source_path: Path,
    *,
    source_specification: SalesOpSourceSpecification,
) -> ParsedSalesOpFile:
    with _workbook_warnings_suppressed():
        workbook = _load_workbook(source_path)
    try:
        worksheet = _first_visible_sheet(workbook)
        marker_row, marker_column = _find_plan_marker(worksheet)
        plan_date = _parse_plan_date(worksheet.cell(marker_row, marker_column + 1).value)
        modified_at = workbook.properties.modified or datetime.fromtimestamp(source_path.stat().st_mtime)  # noqa: DTZ006
        header_row = marker_row + 1
        headers = tuple(cell.value for cell in next(worksheet.iter_rows(min_row=header_row, max_row=header_row)))
        first_column, last_column = _find_header_bounds(headers)
        bound_columns, missing_headers = _bind_source_columns(
            source_specification.mappings,
            headers=tuple(headers[first_column : last_column + 1]),
            first_column=first_column,
        )

        rows: list[DbRow] = []
        warning_count = len(missing_headers)
        if missing_headers:
            LOGGER.warning(
                "План продаж: в файле %s отсутствуют столбцы из спецификации: %s. Значения заменены на NULL.",
                source_path,
                ", ".join(repr(header) for header in missing_headers),
            )
        for row_number, raw_row in enumerate(
            worksheet.iter_rows(min_row=header_row + 1, values_only=True),
            start=header_row + 1,
        ):
            prepared_row, row_warning_count = _prepare_row(
                raw_row,
                row_number=row_number,
                context=_RowContext(source_specification.mappings, bound_columns, plan_date, str(source_path)),
            )
            warning_count += row_warning_count
            if prepared_row.get(PRODUCER_TARGET) is None or prepared_row.get(SELLER_TARGET) is None:
                continue
            rows.append(prepared_row)

        return ParsedSalesOpFile(
            plan_date=plan_date,
            modified_at=modified_at,
            sheet_name=worksheet.title,
            rows=rows,
            warning_count=warning_count,
        )
    finally:
        workbook.close()


def _load_workbook(source_path: Path):  # noqa: ANN202
    try:
        return load_workbook(source_path, read_only=True, data_only=True, keep_links=False)
    except Exception as error:
        message = f"не удалось открыть Excel-файл: {error}"
        raise sales_op_error(message) from error


def _first_visible_sheet(workbook):  # noqa: ANN001, ANN202
    worksheet = next((sheet for sheet in workbook.worksheets if sheet.sheet_state == "visible"), None)
    if worksheet is None:
        message = "в книге нет видимых листов"
        raise sales_op_error(message)
    return worksheet


def _find_plan_marker(worksheet: Worksheet) -> tuple[int, int]:
    for row in worksheet.iter_rows():
        for cell in row:
            if normalize_excel_header(cell.value).rstrip(":") == PLAN_MARKER:
                return cell.row, cell.column
    message = "на первом видимом листе не найден маркер 'План продаж :'"
    raise sales_op_error(message)


def _parse_plan_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, int | float):
        converted = from_excel(value)
        return converted.date() if isinstance(converted, datetime) else converted
    if isinstance(value, str):
        for date_format in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m"):
            try:
                return datetime.strptime(value.strip(), date_format).date()  # noqa: DTZ007
            except ValueError:
                continue
    cell_reference = "ячейка справа от 'План продаж :'"
    raise CellTypeConversionError(cell_reference, "date", value)


def _find_header_bounds(headers: tuple[object, ...]) -> tuple[int, int]:
    normalized = tuple(normalize_excel_header(header) for header in headers)
    try:
        first_column = normalized.index(PRODUCER_HEADER)
        last_column = normalized.index(COMMENTS_HEADER, first_column)
    except ValueError as error:
        message = "в строке заголовков не найден диапазон от 'Производитель' до 'Комментарии'"
        raise sales_op_error(message) from error
    return first_column, last_column


def _bind_source_columns(
    mappings: tuple[SalesOpColumnMapping, ...],
    *,
    headers: tuple[object, ...],
    first_column: int,
) -> tuple[dict[str, int], tuple[str, ...]]:
    positions: dict[str, list[int]] = defaultdict(list)
    for relative_index, header in enumerate(headers):
        normalized = normalize_excel_header(header)
        if normalized:
            positions[normalized].append(first_column + relative_index)

    grouped_mappings: dict[str, list[SalesOpColumnMapping]] = defaultdict(list)
    for mapping in mappings:
        if mapping.mode == "source" and mapping.source_name is not None:
            grouped_mappings[normalize_excel_header(mapping.source_name)].append(mapping)

    bound_columns: dict[str, int] = {}
    missing_headers: list[str] = []
    for normalized_name, source_mappings in grouped_mappings.items():
        source_positions = positions.get(normalized_name, [])
        if not source_positions:
            missing_headers.append(source_mappings[0].source_name or normalized_name)
            continue
        for index, mapping in enumerate(source_mappings):
            position = source_positions[index] if index < len(source_positions) else source_positions[0]
            bound_columns[mapping.column.name] = position

    return bound_columns, tuple(missing_headers)


def _prepare_row(
    raw_row: tuple[object, ...],
    *,
    row_number: int,
    context: _RowContext,
) -> tuple[DbRow, int]:
    prepared: DbRow = {}
    warning_count = 0
    for mapping in context.mappings:
        if mapping.mode == "plan_date":
            prepared[mapping.column.name] = context.plan_date
            continue
        if mapping.mode != "source":
            continue
        column_index = context.bound_columns.get(mapping.column.name)
        if column_index is None:
            prepared[mapping.column.name] = None
            continue
        value = raw_row[column_index] if column_index < len(raw_row) else None
        if isinstance(value, str) and value.strip().upper() in EXCEL_ERROR_VALUES:
            warning_count += 1
        prepared[mapping.column.name] = convert_excel_value(
            value,
            kind=mapping.column.kind,
            cell_reference=f"{get_column_letter(column_index + 1)}{row_number}",
            excel_errors_as_null=True,
            source_name=context.source_name,
        )
    return prepared, warning_count


@dataclass(frozen=True, slots=True)
class _RowContext:
    mappings: tuple[SalesOpColumnMapping, ...]
    bound_columns: dict[str, int]
    plan_date: date
    source_name: str


@contextmanager
def _workbook_warnings_suppressed():  # noqa: ANN202
    with catch_warnings():
        simplefilter("ignore", UserWarning)
        yield
