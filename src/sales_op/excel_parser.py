import re
import warnings
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import from_excel
from python_calamine import SheetVisibleEnum
from python_calamine import load_workbook as calamine_load_workbook

from app_logging import get_logger
from business_errors import CellTypeConversionError, FileMetadataMissingError
from excel_parser import (
    EXCEL_ERROR_VALUES,
    OPENPYXL_EXTENSION_WARNING,
    ExcelErrorWarnings,
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
    from excel_parser import WorkbookCoreProperties

PLAN_MARKER = "планпродаж"
PRODUCER_HEADER = "производитель"
COMMENTS_HEADER = "комментарии"
PRODUCER_TARGET = "producer_code"
SELLER_TARGET = "seller_code"
CLIENT_NAME_TARGET = "client_name"
# openpyxl emits this UserWarning from worksheet._reader when a date-formatted
# numeric cell holds a serial outside the supported date range; the cell is
# then returned as the error value ``#VALUE!``.
OPENPYXL_DATE_WARNING = r"is marked as a date but the serial value .* is outside the limits for dates"
LOGGER = get_logger("sales_op")


def inspect_sales_op_workbook(
    source_path: Path,
    *,
    timezone: tzinfo,
) -> tuple[date, datetime, WorkbookMetadata]:
    """
    Plan date, workbook modification moment and metadata for one source file.

    The date is read by calamine (~4 ms versus ~100 ms for an openpyxl
    read-only open); the parsing path below stays on openpyxl, so the
    inspection never loads the full book. Core properties are read once and
    serve both ``file_modifier`` and ``modified_at``.
    """
    plan_date = _read_plan_date(source_path)
    core_properties = read_core_properties(source_path)
    metadata = read_workbook_metadata(source_path, timezone=timezone, core_properties=core_properties)
    modified_at = _workbook_modified_at(source_path, metadata, core_properties=core_properties)
    return plan_date, modified_at, metadata


def parse_sales_op_workbook(
    source_path: Path,
    *,
    source_specification: SalesOpSourceSpecification,
) -> ParsedSalesOpFile:
    with _workbook_warnings_suppressed(source_path):
        workbook = _load_workbook(source_path)
        try:
            worksheet = _first_visible_sheet(workbook)
            marker_row, marker_column = _find_plan_marker(worksheet)
            plan_date = _parse_plan_date(worksheet.cell(marker_row, marker_column + 1).value)
            excel_error_warnings = ExcelErrorWarnings(source_name=str(source_path))
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
            producer_index = bound_columns.get(PRODUCER_TARGET)
            seller_index = bound_columns.get(SELLER_TARGET)
            client_name_index = bound_columns.get(CLIENT_NAME_TARGET)
            try:
                for row_number, raw_row in enumerate(
                    worksheet.iter_rows(min_row=header_row + 1, values_only=True),
                    start=header_row + 1,
                ):
                    if all(_is_blank_cell(value) for value in raw_row):
                        # A fully empty row ends the plan block; everything after it
                        # (service rows, totals) is not data.
                        break
                    if _is_non_plan_row(
                        raw_row,
                        producer_index=producer_index,
                        seller_index=seller_index,
                        client_name_index=client_name_index,
                    ):
                        continue
                    prepared_row = _prepare_row(
                        raw_row,
                        row_number=row_number,
                        context=_RowContext(
                            source_specification.mappings,
                            bound_columns,
                            plan_date,
                            str(source_path),
                            excel_error_warnings,
                        ),
                    )
                    _require_not_nullable(
                        prepared_row,
                        source_specification.mappings,
                        row_number=row_number,
                        source_name=str(source_path),
                    )
                    rows.append(prepared_row)
            finally:
                excel_error_warnings.flush()

            return ParsedSalesOpFile(
                plan_date=plan_date,
                sheet_name=worksheet.title,
                rows=rows,
                warning_count=excel_error_warnings.count,
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


def _workbook_modified_at(
    source_path: Path,
    metadata: WorkbookMetadata,
    *,
    core_properties: WorkbookCoreProperties | None = None,
) -> datetime:
    """
    Naive workbook modification moment for the source history.

    Openpyxl's ``properties.modified`` is replaced by the shared
    ``docProps/core.xml`` reader. A package without a readable core part
    (some workbook writers leave it out) falls back to the file mtime, the
    way the openpyxl ``None`` used to. Aware timestamps are normalized to
    naive UTC like openpyxl did; a timezone-free value keeps its own hour.

    ``core_properties`` is passed by callers that already read the part (the
    inspection reads it once for ``file_modifier`` and ``modified``); the
    argument keeps the helper usable on its own.
    """
    if core_properties is None:
        try:
            core_properties = read_core_properties(source_path)
        except FileMetadataMissingError:
            return metadata.file_modified_at.replace(tzinfo=None)
    modified_at = core_properties.modified
    if isinstance(modified_at, datetime):
        if modified_at.tzinfo is None:
            return modified_at
        return modified_at.astimezone(UTC).replace(tzinfo=None)
    return metadata.file_modified_at.replace(tzinfo=None)


def _read_plan_date(source_path: Path) -> date:
    """
    Plan date from the first visible sheet.

    Calamine reads the marker row in a few milliseconds, while an openpyxl
    read-only open costs ~100 ms per candidate file. Any calamine problem
    (unsupported package, missing marker, unexpected value shape) falls back
    to the openpyxl reader, which stays the authority for sales_op values;
    business errors are therefore raised by openpyxl, never by the fast path.
    """
    try:
        return _read_plan_date_with_calamine(source_path)
    except Exception as error:  # noqa: BLE001 - any calamine failure falls back to openpyxl
        LOGGER.warning(
            "План продаж: calamine не прочитал дату плана в %s (%s: %s); повтор через openpyxl.",
            source_path,
            type(error).__name__,
            error,
        )
        return _read_plan_date_with_openpyxl(source_path)


def _read_plan_date_with_calamine(source_path: Path) -> date:
    workbook = calamine_load_workbook(source_path)
    visible_sheets = [sheet for sheet in workbook.sheets_metadata if sheet.visible == SheetVisibleEnum.Visible]
    if not visible_sheets:
        message = "в книге нет видимых листов"
        raise sales_op_error(message)
    rows = workbook.get_sheet_by_name(visible_sheets[0].name).to_python(skip_empty_area=False)
    for row in rows:
        for column_index, value in enumerate(row):
            if normalize_excel_header(value).rstrip(":") == PLAN_MARKER:
                date_index = column_index + 1
                date_value = row[date_index] if date_index < len(row) else None
                return _parse_plan_date(date_value)
    message = "на первом видимом листе не найден маркер 'План продаж :'"
    raise sales_op_error(message)


def _read_plan_date_with_openpyxl(source_path: Path) -> date:
    with _workbook_warnings_suppressed(source_path):
        workbook = _load_workbook(source_path)
        try:
            worksheet = _first_visible_sheet(workbook)
            marker_row, marker_column = _find_plan_marker(worksheet)
            return _parse_plan_date(worksheet.cell(marker_row, marker_column + 1).value)
        finally:
            workbook.close()


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
    client_name_index: int | None,
) -> bool:
    return (
        _is_blank_cell(_cell_at(raw_row, producer_index))
        or _is_blank_cell(_cell_at(raw_row, seller_index))
        or _is_blank_cell(_cell_at(raw_row, client_name_index))
    )


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
) -> DbRow:
    prepared: DbRow = {}
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
        prepared[mapping.column.name] = convert_excel_value(
            value,
            kind=mapping.column.kind,
            cell_reference=f"{get_column_letter(column_index + 1)}{row_number}",
            excel_errors_as_null=True,
            source_name=context.source_name,
            excel_error_warnings=context.excel_error_warnings,
        )
    return prepared


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
    excel_error_warnings: ExcelErrorWarnings


@contextmanager
def _workbook_warnings_suppressed(source_path: Path) -> Generator[None]:
    # ``load_openpyxl_workbook`` filters the "Unknown extension" warning during
    # ``load_workbook``, but a ``read_only=True`` workbook parses its sheet
    # lazily: both that warning and the "serial value outside the limits for
    # dates" warning fire from ``openpyxl.worksheet._reader`` during
    # ``iter_rows``/cell access, after ``load_openpyxl_workbook`` has returned.
    # This context wraps the whole workbook lifecycle so the lazy-read warnings
    # stay covered.
    #
    # Both known openpyxl warnings are captured (``record=True`` keeps them
    # from openpyxl's default stderr output). The extension warning is dropped;
    # the date warning is re-emitted by our own logger with the source file
    # name (openpyxl's text has none) — the cell itself is returned as
    # ``#VALUE!`` and goes through the usual excel-error-to-NULL path, so no
    # data is silently lost. Every other warning is re-issued after the scope,
    # so nothing unrelated is masked and no global filter state is installed.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield

    for warning in caught:
        message = str(warning.message)
        if warning.category is UserWarning and re.search(OPENPYXL_EXTENSION_WARNING, message):
            continue
        if warning.category is UserWarning and re.search(OPENPYXL_DATE_WARNING, message):
            LOGGER.warning("План продаж: openpyxl в файле %s: %s", source_path, message)
            continue
        warnings.warn_explicit(
            warning.message,
            warning.category,
            filename=warning.filename,
            lineno=warning.lineno,
        )
