from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from warnings import catch_warnings, filterwarnings

from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import from_excel

from app_logging import get_logger
from business_errors import CellTypeConversionError, FileMetadataMissingError
from excel_parser import (
    EXCEL_ERROR_VALUES,
    OPENPYXL_EXTENSION_WARNING,
    convert_excel_value,
    load_openpyxl_workbook,
    read_core_properties,
    read_workbook_metadata,
)
from sales_op.models import (
    ParsedSalesOpFile,
    SalesOpColumnMapping,
    SalesOpSourceSpecification,
    sales_op_error,
)
from sales_op.specification import normalize_excel_header

if TYPE_CHECKING:
    from collections.abc import Generator
    from datetime import tzinfo
    from pathlib import Path

    from openpyxl.workbook.workbook import Workbook
    from openpyxl.worksheet.worksheet import Worksheet

    from dictionary_models import DbRow, WorkbookMetadata

PLAN_MARKER = "планпродаж"
PRODUCER_HEADER = "производитель"
COMMENTS_HEADER = "комментарии"
PRODUCER_TARGET = "producer_code"
SELLER_TARGET = "seller_code"
LOGGER = get_logger("sales_op")


def inspect_sales_op_workbook(
    source_path: Path,
    *,
    timezone: tzinfo,
) -> tuple[date, datetime, WorkbookMetadata]:
    with _workbook_warnings_suppressed():
        workbook = _load_workbook(source_path)
        try:
            worksheet = _first_visible_sheet(workbook)
            marker_row, marker_column = _find_plan_marker(worksheet)
            plan_date = _parse_plan_date(worksheet.cell(marker_row, marker_column + 1).value)
            metadata = read_workbook_metadata(source_path, timezone=timezone)
            modified_at = _workbook_modified_at(source_path, metadata)
            return plan_date, modified_at, metadata
        finally:
            workbook.close()


def parse_sales_op_workbook(
    source_path: Path,
    *,
    source_specification: SalesOpSourceSpecification,
    metadata: WorkbookMetadata | None = None,
) -> ParsedSalesOpFile:
    with _workbook_warnings_suppressed():
        workbook = _load_workbook(source_path)
        try:
            worksheet = _first_visible_sheet(workbook)
            marker_row, marker_column = _find_plan_marker(worksheet)
            plan_date = _parse_plan_date(worksheet.cell(marker_row, marker_column + 1).value)
            workbook_metadata = metadata or read_workbook_metadata(source_path, timezone=UTC)
            modified_at = _workbook_modified_at(source_path, workbook_metadata)
            header_row = marker_row + 1
            headers = tuple(cell.value for cell in next(worksheet.iter_rows(min_row=header_row, max_row=header_row)))
            first_column, last_column = _find_header_bounds(headers)
            bound_columns, missing_headers = _bind_source_columns(
                source_specification.mappings,
                headers=tuple(headers[first_column : last_column + 1]),
                first_column=first_column,
            )
            if missing_headers:
                message = f"в файле {source_path} отсутствуют обязательные столбцы из спецификации: " + ", ".join(
                    repr(header) for header in missing_headers
                )
                raise sales_op_error(message)

            rows: list[DbRow] = []
            warning_count = 0
            producer_index = bound_columns.get(PRODUCER_TARGET)
            seller_index = bound_columns.get(SELLER_TARGET)
            for row_number, raw_row in enumerate(
                worksheet.iter_rows(min_row=header_row + 1, values_only=True),
                start=header_row + 1,
            ):
                if _is_non_plan_row(raw_row, producer_index=producer_index, seller_index=seller_index):
                    continue
                prepared_row, row_warning_count = _prepare_row(
                    raw_row,
                    row_number=row_number,
                    context=_RowContext(source_specification.mappings, bound_columns, plan_date, str(source_path)),
                )
                warning_count += row_warning_count
                _require_not_nullable(
                    prepared_row,
                    source_specification.mappings,
                    row_number=row_number,
                    source_name=str(source_path),
                )
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


def _load_workbook(source_path: Path) -> Workbook:
    try:
        return load_openpyxl_workbook(source_path, data_only=True, read_only=True, keep_links=False)
    except Exception as error:
        message = f"не удалось открыть Excel-файл: {error}"
        raise sales_op_error(message) from error


def _first_visible_sheet(workbook: Workbook) -> Worksheet:
    worksheet = next((sheet for sheet in workbook.worksheets if sheet.sheet_state == "visible"), None)
    if worksheet is None:
        message = "в книге нет видимых листов"
        raise sales_op_error(message)
    return worksheet


def _workbook_modified_at(source_path: Path, metadata: WorkbookMetadata) -> datetime:
    """
    Naive workbook modification moment for the source history.

    Openpyxl's ``properties.modified`` is replaced by the shared
    ``docProps/core.xml`` reader. A package without a readable core part
    (some workbook writers leave it out) falls back to the file mtime, the
    way the openpyxl ``None`` used to. Aware timestamps are normalized to
    naive UTC like openpyxl did; a timezone-free value keeps its own hour.
    """
    try:
        modified_at = read_core_properties(source_path).modified
    except FileMetadataMissingError:
        modified_at = None
    if isinstance(modified_at, datetime):
        if modified_at.tzinfo is None:
            return modified_at
        return modified_at.astimezone(UTC).replace(tzinfo=None)
    return metadata.file_modified_at.replace(tzinfo=None)


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
                return date.strptime(value.strip(), date_format)
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
            # An optional column may be absent (its values become NULL); a
            # "Not Nullable" column must exist in the workbook.
            if any(mapping.required for mapping in source_mappings):
                missing_headers.append(source_mappings[0].source_name or normalized_name)
            continue
        for index, mapping in enumerate(source_mappings):
            position = source_positions[index] if index < len(source_positions) else source_positions[0]
            bound_columns[mapping.column.name] = position

    return bound_columns, tuple(missing_headers)


def _is_non_plan_row(
    raw_row: tuple[object, ...],
    *,
    producer_index: int | None,
    seller_index: int | None,
) -> bool:
    return _is_blank_cell(_cell_at(raw_row, producer_index)) or _is_blank_cell(_cell_at(raw_row, seller_index))


def _cell_at(raw_row: tuple[object, ...], index: int | None) -> object | None:
    if index is None or index >= len(raw_row):
        return None
    return raw_row[index]


def _is_blank_cell(value: object) -> bool:
    # Mirrors conditions when convert_excel_value returns NULL:
    # None, empty string, single "-", or Excel error literal.
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return stripped in {"", "-"} or stripped.upper() in EXCEL_ERROR_VALUES
    return False


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


def _require_not_nullable(
    row: DbRow,
    mappings: tuple[SalesOpColumnMapping, ...],
    *,
    row_number: int,
    source_name: str,
) -> None:
    for mapping in mappings:
        if not mapping.required or mapping.mode not in {"source", "plan_date"}:
            continue
        if row.get(mapping.column.name) is None:
            message = f"в файле {source_name} обязательное поле {mapping.column.name!r} пусто в строке {row_number}"
            raise sales_op_error(message)


@dataclass(frozen=True, slots=True)
class _RowContext:
    mappings: tuple[SalesOpColumnMapping, ...]
    bound_columns: dict[str, int]
    plan_date: date
    source_name: str


@contextmanager
def _workbook_warnings_suppressed() -> Generator[None]:
    # ``load_openpyxl_workbook`` filters this warning during ``load_workbook``,
    # but a ``read_only=True`` workbook parses its sheet lazily: the
    # ``"Unknown extension … will be removed"`` warning fires from
    # ``openpyxl.worksheet._reader`` during ``iter_rows``/cell access, after
    # ``load_openpyxl_workbook`` has returned. This context wraps the whole
    # workbook lifecycle so the lazy-read warnings stay covered.
    #
    # Narrow on message+category+module: a blanket ``simplefilter("ignore",
    # UserWarning)`` would also mask genuine conversion warnings.
    with catch_warnings():
        filterwarnings(
            "ignore",
            message=OPENPYXL_EXTENSION_WARNING,
            category=UserWarning,
            module=r"openpyxl\.worksheet\._reader",
        )
        yield
