import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, tzinfo
from numbers import Number
from typing import TYPE_CHECKING
from warnings import catch_warnings, filterwarnings
from zipfile import BadZipFile, ZipFile

import pandas as pd
from defusedxml.ElementTree import ParseError, fromstring
from openpyxl import load_workbook

from app_logging import get_logger
from business_errors import (
    CellTypeConversionError,
    DuplicateCellValueError,
    DuplicatePrimaryKeyError,
    FileMetadataMissingError,
    RequiredCellValueError,
    SourceColumnMissingError,
)
from dictionary_models import ColumnDefinition, ColumnKind, DbRow, DbValue, DictionaryPayload, WorkbookMetadata
from source_resolver import SourceFileNotFoundError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from openpyxl.workbook.workbook import Workbook

TEXT_COLUMN_KIND = "text"
OOXML_CORE_PROPERTIES_PATH = "docProps/core.xml"
OOXML_NAMESPACES = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}
EXCEL_ERROR_VALUES = {"#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A", "#GETTING_DATA"}
ORIGIN_ATTR = "xls_origin"
OPENPYXL_EXTENSION_WARNING = ".* extension is not supported and will be removed"
# pandas engine for every dictionary value frame: the native `calamine` engine
# (python-calamine) reads `.xlsx` and `.xlsb` with value parity versus the
# previous openpyxl/pyxlsb reads (verified over `mock_data/`), while the
# pyxlsb engine is deprecated in pandas.
EXCEL_ENGINE = "calamine"
type CellValueConverter = Callable[[object], DbValue]
LOGGER = get_logger("excel")


@dataclass(frozen=True, slots=True)
class FrameOrigin:
    """
    Original workbook coordinates of a parsed frame.

    Keeps the sheet name, the 1-based Excel row of the header and the original
    Excel letter of every column, so errors and warnings can point at the real
    cell in the source document even after columns were selected or renamed.
    """

    sheet_name: str
    header_row: int
    column_letters: dict[str, str]

    def cell_reference(self, column_name: str, row_position: int) -> str | None:
        letter = self.column_letters.get(column_name)
        if letter is None:
            return None
        return f"{letter}{self.header_row + 1 + row_position}"


def attach_frame_origin(
    frame: pd.DataFrame,
    *,
    sheet_name: str,
    header_row: int,
    column_letters: dict[str, str],
) -> pd.DataFrame:
    frame.attrs[ORIGIN_ATTR] = FrameOrigin(
        sheet_name=sheet_name,
        header_row=header_row,
        column_letters=column_letters,
    )
    return frame


def frame_origin(frame: pd.DataFrame) -> FrameOrigin | None:
    origin = frame.attrs.get(ORIGIN_ATTR)
    return origin if isinstance(origin, FrameOrigin) else None


@dataclass(slots=True)
class ExcelErrorWarnings:
    """
    Per-file collector for Excel-error cells replaced with NULL.

    Logging every cell of a large sheet floods the log with one line per
    ``#N/A``/``#VALUE!``/… value, so the collector counts them all and keeps
    the first ``sample_limit`` coordinates; ``flush`` emits one summary line.
    A loader that does not pass a collector keeps the per-cell log line.
    """

    source_name: str | None = None
    sample_limit: int = 5
    count: int = 0
    samples: list[tuple[str, object]] = field(default_factory=list)

    def record(self, value: object, cell_reference: str) -> None:
        self.count += 1
        if len(self.samples) < self.sample_limit:
            self.samples.append((cell_reference, value))

    def flush(self) -> int:
        if self.count == 0:
            return 0
        samples = "; ".join(f"{value!r} в {cell_reference}" for cell_reference, value in self.samples)
        remaining = self.count - len(self.samples)
        tail = f"; и ещё {remaining}" if remaining > 0 else ""
        LOGGER.warning(
            "Excel-файл %s: %s ошибочных значений заменено на NULL: %s%s.",
            self.source_name or "<неизвестный источник>",
            self.count,
            samples,
            tail,
        )
        return self.count


def build_cell_locations(
    *,
    sheet_name: str,
    header_row: int,
    column_letters: Mapping[str, str],
    row_count: int,
) -> list[dict[str, str]]:
    """
    Per-row mapping of column name to the original ``sheet!CELL`` reference.

    ``header_row`` is the 1-based Excel row of the header; the first data row is
    ``header_row + 1``.
    """
    return [
        {
            column_name: f"{sheet_name}!{letter}{header_row + 1 + row_position}"
            for column_name, letter in column_letters.items()
        }
        for row_position in range(row_count)
    ]


def load_openpyxl_workbook(
    source_path: Path,
    *,
    data_only: bool,
    read_only: bool = False,
    keep_links: bool = True,
) -> Workbook:
    """
    Open an openpyxl workbook with the openpyxl extension warning suppressed.

    openpyxl 3.1 reads every format but emits ``"Unknown extension … will be
    removed"`` for parts it ignores (e.g. inside ``.xlsb`` mocks). The
    warning is silenced with a scoped ``catch_warnings`` block instead of a
    module-level ``filterwarnings``. Parsing
    modes stay per-caller: PD loads fully materialized books, sales_op opens
    read-only value copies.
    """
    with catch_warnings():
        filterwarnings(
            "ignore",
            message=OPENPYXL_EXTENSION_WARNING,
            category=UserWarning,
            module=r"openpyxl\.worksheet\._reader",
        )
        return load_workbook(source_path, data_only=data_only, read_only=read_only, keep_links=keep_links)


@dataclass(frozen=True, slots=True)
class WorkbookCoreProperties:
    """
    Core properties of a zip-based OOXML package (``docProps/core.xml``).

    The same part carries the workbook metadata for every zip-based format
    (``.xlsx``/``.xlsm``/``.xlsb``): creator/last-modified-by for source
    history plus the created/modified timestamps. ``created``/``modified``
    are ``datetime.fromisoformat`` parses of the XML values and keep the
    stored offset when one is present; fields absent from the package are
    ``None``.
    """

    creator: str | None
    last_modified_by: str | None
    created: datetime | None
    modified: datetime | None


def read_core_properties(source_path: Path) -> WorkbookCoreProperties:
    """
    Read ``docProps/core.xml`` from an OOXML package.

    The shared metadata reader for the calamine step: one zip part serves
    every format, so openpyxl is not opened just for metadata. The file is
    reopened by path even when a workbook copy is already open — share-read
    is safe and no handles are kept.
    """
    try:
        with ZipFile(source_path) as workbook_archive:
            root = fromstring(workbook_archive.read(OOXML_CORE_PROPERTIES_PATH))
    except (BadZipFile, KeyError, ParseError) as error:
        raise FileMetadataMissingError from error

    return WorkbookCoreProperties(
        creator=root.findtext("dc:creator", namespaces=OOXML_NAMESPACES),
        last_modified_by=root.findtext("cp:lastModifiedBy", namespaces=OOXML_NAMESPACES),
        created=_parse_core_timestamp(root.findtext("dcterms:created", namespaces=OOXML_NAMESPACES)),
        modified=_parse_core_timestamp(root.findtext("dcterms:modified", namespaces=OOXML_NAMESPACES)),
    )


def _parse_core_timestamp(timestamp: str | None) -> datetime | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromisoformat(timestamp)
    except ValueError as error:
        raise FileMetadataMissingError from error


def read_workbook_metadata(
    source_path: Path,
    *,
    timezone: tzinfo,
    core_properties: WorkbookCoreProperties | None = None,
) -> WorkbookMetadata:
    """
    Workbook metadata for the source history.

    ``read_core_properties`` serves every zip-based OOXML format, so
    ``file_modifier`` comes from the same ``docProps/core.xml`` part for
    ``.xlsx`` and ``.xlsb``. A caller that already read the core properties
    (sales_op needs ``modified`` too) passes them in — the package is opened
    once, not twice.
    """
    if not source_path.exists():
        raise SourceFileNotFoundError(source_path)

    properties = core_properties if core_properties is not None else read_core_properties(source_path)
    return WorkbookMetadata(
        file_modifier=_extract_user_name(
            last_modified_by=properties.last_modified_by,
            creator=properties.creator,
        ),
        file_modified_at=datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone),
        loaded_at=datetime.now(tz=timezone),
    )


def read_single_sheet_frame(
    source_path: Path,
    *,
    timezone: tzinfo,
    sheet_name: str | int = 0,
) -> tuple[pd.DataFrame, WorkbookMetadata]:
    """
    Read one sheet plus its metadata.

    One ``pandas.ExcelFile`` handle serves the sheet names, the first-row probe
    (needed for the real column letters) and the value frame, so the workbook
    package is opened once per loader instead of once per call.
    """
    metadata = read_workbook_metadata(source_path, timezone=timezone)
    with pd.ExcelFile(source_path, engine=EXCEL_ENGINE) as workbook:
        sheet_names = tuple(workbook.sheet_names)
        first_row = pd.read_excel(
            workbook,
            sheet_name=sheet_name,
            header=None,
            nrows=1,
            keep_default_na=False,
            na_filter=False,
        )
        frame = pd.read_excel(
            workbook,
            sheet_name=sheet_name,
            keep_default_na=False,
            na_filter=False,
        )
    first_column_index = _first_used_column_index(first_row)
    column_letters = {
        str(column): _excel_column_name(first_column_index + column_index + 1)
        for column_index, column in enumerate(frame.columns)
    }
    return (
        attach_frame_origin(
            frame,
            sheet_name=_resolved_sheet_name(sheet_names, sheet_name=sheet_name),
            header_row=1,
            column_letters=column_letters,
        ),
        metadata,
    )


def _first_used_column_index(first_row: pd.DataFrame) -> int:
    if first_row.empty:
        return 0
    for column_index, value in enumerate(first_row.iloc[0]):
        if str(value).strip():
            return column_index
    return 0


def _resolved_sheet_name(sheet_names: Sequence[str], *, sheet_name: str | int) -> str:
    if isinstance(sheet_name, str):
        return sheet_name
    if 0 <= sheet_name < len(sheet_names):
        return sheet_names[sheet_name]
    return str(sheet_name)


def select_columns_between(
    frame: pd.DataFrame,
    *,
    start_column: str,
    end_column: str,
) -> pd.DataFrame:
    headers = tuple(str(column) for column in frame.columns)
    if start_column not in headers:
        raise SourceColumnMissingError(start_column)
    if end_column not in headers:
        raise SourceColumnMissingError(end_column)

    start_index = headers.index(start_column)
    end_index = headers.index(end_column)
    selected_headers = headers[start_index : end_index + 1]
    return frame.loc[:, list(selected_headers)].copy()


def select_columns_excluding_checks(frame: pd.DataFrame) -> pd.DataFrame:
    selected_headers = [str(column) for column in frame.columns if not _is_check_column(str(column))]
    return frame.loc[:, selected_headers].copy()


def build_dictionary_payload(  # noqa: PLR0913  # stable kwargs API for every loader kind; the body is split below
    *,
    table_reference: str,
    selected_frame: pd.DataFrame,
    primary_key_columns: tuple[str, ...],
    metadata: WorkbookMetadata,
    not_null_filtered_columns: tuple[str, ...] = (),
    not_null_columns: tuple[str, ...] = (),
    unique_columns: tuple[str, ...] = (),
    column_kinds: dict[str, ColumnKind] | None = None,
    source_name: str | None = None,
    preserve_text_values: bool = False,
    column_aliases: dict[str, str] | None = None,
    column_converters: dict[str, CellValueConverter] | None = None,
    where_columns: dict[str, object] | None = None,
    dropped_columns: tuple[str, ...] = (),
    cell_locations: Sequence[Mapping[str, str]] | None = None,
) -> DictionaryPayload:
    """
    Validate the column contract, convert rows and assemble the dictionary payload.

    The per-row order is frozen (see ``test_build_dictionary_payload_row_order``):
    ``where`` filter → raw skip → convert (excel-error warnings, custom
    converters, required-null deferral, unique-column duplicates) → converted
    skip → primary-key duplicate. Aliases are applied before every other
    check, so ``*_columns`` arguments are expressed in target column names.
    """
    if column_aliases:
        selected_frame = _apply_column_aliases(selected_frame, column_aliases=column_aliases)
        selected_frame = _rename_frame_origin(selected_frame, column_aliases=column_aliases)
        cell_locations = _rename_locations(cell_locations, column_aliases=column_aliases)
    selected_columns = tuple(str(column) for column in selected_frame.columns)
    # Historically the contract is reported in this order: primary key and
    # filtered columns first, then not-null / unique / where columns.
    _validate_column_contract(
        selected_columns=selected_columns,
        required_columns=(
            *primary_key_columns,
            *not_null_filtered_columns,
            *not_null_columns,
            *unique_columns,
            *(where_columns or {}),
        ),
    )

    target_columns = tuple(column_name for column_name in selected_columns if column_name not in dropped_columns)
    origin = frame_origin(selected_frame)
    configured_column_kinds = column_kinds or {}
    configured_column_converters = column_converters or {}
    excel_error_warnings = ExcelErrorWarnings(source_name=source_name)
    converter = _RowConverter(
        selected_columns=selected_columns,
        primary_key_columns=primary_key_columns,
        not_null_columns=not_null_columns,
        dropped_columns=dropped_columns,
        column_kinds=configured_column_kinds,
        column_converters=configured_column_converters,
        preserve_text_values=preserve_text_values,
        source_name=source_name,
        origin=origin,
        unique_columns=unique_columns,
        excel_error_warnings=excel_error_warnings,
    )

    rows: list[DbRow] = []
    try:
        for row_number, row in enumerate(selected_frame.itertuples(index=False, name=None), start=2):
            row_position = row_number - 2
            row_locations = (
                cell_locations[row_position]
                if cell_locations is not None and row_position < len(cell_locations)
                else None
            )
            raw_row = dict(zip(selected_columns, row, strict=True))
            if not _row_matches_where(raw_row, where_columns):
                continue
            if _should_skip_raw_filtered_row(raw_row, not_null_filtered_columns):
                continue
            row_reference = _row_reference(origin, row_locations=row_locations, row_number=row_number)
            prepared_row = converter.convert(
                row,
                row_number=row_number,
                row_position=row_position,
                row_locations=row_locations,
            )
            if _should_skip_filtered_row(prepared_row, not_null_filtered_columns):
                continue
            converter.register_primary_key(prepared_row, row_number=row_number, row_reference=row_reference)
            rows.append(prepared_row)

        converter.raise_deferred_required_error()
    finally:
        excel_error_warnings.flush()

    columns = _build_column_definitions(
        selected_columns=target_columns,
        primary_key_columns=primary_key_columns,
        column_kinds=configured_column_kinds,
        not_null_columns=not_null_columns,
        unique_columns=unique_columns,
    )
    return DictionaryPayload(
        table_reference=table_reference,
        primary_key_columns=primary_key_columns,
        columns=columns,
        rows=rows,
        metadata=metadata,
        warning_count=converter.warning_count,
    )


def _validate_column_contract(
    *,
    selected_columns: tuple[str, ...],
    required_columns: tuple[str, ...],
) -> None:
    """
    Every configured column must exist among the (already aliased) source columns.

    Aliases are applied before this check, so ``required_columns`` is
    expressed in target column names; a missing source column is an error
    (AGENTS.md landmine). The caller assembles the tuple in the historical
    reporting order: primary key, filtered, not-null, unique, where columns.
    """
    for required_column in required_columns:
        if required_column not in selected_columns:
            raise SourceColumnMissingError(required_column)


@dataclass(slots=True)
class _RowConverter:
    """
    Per-row conversion state and logic for ``build_dictionary_payload``.

    Owns the per-column pipeline — excel-error warning counting, custom
    converters, required-null deferral, unique-column duplicate detection —
    and the primary-key duplicate check. The caller keeps the surrounding
    order (``where`` filter, raw skip, converted skip).
    """

    selected_columns: tuple[str, ...]
    primary_key_columns: tuple[str, ...]
    not_null_columns: tuple[str, ...]
    dropped_columns: tuple[str, ...]
    column_kinds: dict[str, ColumnKind]
    column_converters: dict[str, CellValueConverter]
    preserve_text_values: bool
    source_name: str | None
    origin: FrameOrigin | None
    unique_columns: tuple[str, ...]
    excel_error_warnings: ExcelErrorWarnings

    warning_count: int = 0
    first_required_error: RequiredCellValueError | None = None
    unique_keys: set[tuple[object, ...]] = field(default_factory=set)
    unique_values: dict[str, dict[object, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.unique_values = {column_name: {} for column_name in self.unique_columns}

    def convert(
        self,
        raw_values: tuple[object, ...],
        *,
        row_number: int,
        row_position: int,
        row_locations: Mapping[str, str] | None,
    ) -> DbRow:
        """Convert one already where-filtered raw row; may raise the row-column duplicate error."""
        prepared_row: DbRow = {}
        for column_index, (column_name, value) in enumerate(
            zip(self.selected_columns, raw_values, strict=True),
            start=1,
        ):
            if column_name in self.dropped_columns:
                continue
            cell_reference = _cell_reference(
                self.origin,
                row_locations=row_locations,
                column_name=column_name,
                row_position=row_position,
                fallback=f"{_excel_column_name(column_index)}{row_number}",
            )
            nullable = column_name not in self.primary_key_columns and column_name not in self.not_null_columns
            custom_converter = self.column_converters.get(column_name)
            if custom_converter is not None:
                prepared_row[column_name] = custom_converter(value)
            else:
                kind = self.column_kinds.get(column_name, TEXT_COLUMN_KIND)
                if _is_excel_error(value):
                    self.warning_count += 1
                prepared_row[column_name] = convert_excel_value(
                    value,
                    kind=kind,
                    cell_reference=cell_reference,
                    excel_errors_as_null=True,
                    source_name=self.source_name,
                    preserve_text=self.preserve_text_values,
                    nullable=nullable,
                    excel_error_warnings=self.excel_error_warnings,
                )
            prepared_value = prepared_row[column_name]
            if not nullable and prepared_value is None and self.first_required_error is None:
                self.first_required_error = RequiredCellValueError(
                    cell_reference,
                    column_name,
                    value,
                    source_name=self.source_name,
                )
            if column_name in self.unique_values and prepared_value is not None:
                first_cell_reference = self.unique_values[column_name].get(prepared_value)
                if first_cell_reference is not None:
                    raise DuplicateCellValueError(
                        column_name,
                        prepared_value,
                        cell_reference,
                        first_cell_reference,
                        source_name=self.source_name,
                    )
                self.unique_values[column_name][prepared_value] = cell_reference
        return prepared_row

    def register_primary_key(self, prepared_row: DbRow, *, row_number: int, row_reference: str | None) -> None:
        """Raise on a converted primary-key collision; called after the converted-skip."""
        if not self.primary_key_columns:
            return
        primary_key_value = tuple(prepared_row[column_name] for column_name in self.primary_key_columns)
        if primary_key_value in self.unique_keys:
            raise DuplicatePrimaryKeyError(
                self.primary_key_columns,
                row_number,
                row_reference=row_reference,
            )
        self.unique_keys.add(primary_key_value)

    def raise_deferred_required_error(self) -> None:
        """Raise the first required-cell error captured during conversion, if any."""
        if self.first_required_error is not None:
            raise self.first_required_error


def _should_skip_filtered_row(row: DbRow, filtered_columns: tuple[str, ...]) -> bool:
    if not filtered_columns:
        return False
    return all(_is_null_like(row[column_name]) for column_name in filtered_columns)


def _should_skip_raw_filtered_row(row: dict[str, object], filtered_columns: tuple[str, ...]) -> bool:
    return bool(filtered_columns) and all(
        normalize_text_value(row[column_name]) is None for column_name in filtered_columns
    )


def _row_matches_where(row: dict[str, object], where_columns: dict[str, object] | None) -> bool:
    if not where_columns:
        return True
    return all(
        normalize_text_value(row[column_name]) == normalize_text_value(expected_value)
        for column_name, expected_value in where_columns.items()
    )


def _is_null_like(value: object) -> bool:
    return value is None or value == ""


def _apply_column_aliases(
    selected_frame: pd.DataFrame,
    *,
    column_aliases: dict[str, str],
) -> pd.DataFrame:
    source_columns = tuple(str(column) for column in selected_frame.columns)
    normalized_aliases = {str(source_name): str(target_name) for source_name, target_name in column_aliases.items()}
    for source_column in normalized_aliases:
        if source_column not in source_columns:
            raise SourceColumnMissingError(source_column)

    renamed_columns = tuple(normalized_aliases.get(source_column, source_column) for source_column in source_columns)
    duplicate_names = sorted({name for name in renamed_columns if renamed_columns.count(name) > 1})
    if duplicate_names:
        duplicate_text = ", ".join(repr(name) for name in duplicate_names)
        msg = f"column_aliases приводит к дублированию целевых столбцов: {duplicate_text}."
        raise ValueError(msg)

    rename_map = {
        source_column: normalized_aliases[source_column]
        for source_column in source_columns
        if source_column in normalized_aliases
    }
    return selected_frame.rename(columns=rename_map)


def _build_column_definitions(
    *,
    selected_columns: tuple[str, ...],
    primary_key_columns: tuple[str, ...],
    column_kinds: dict[str, ColumnKind],
    not_null_columns: tuple[str, ...],
    unique_columns: tuple[str, ...],
) -> tuple[ColumnDefinition, ...]:
    data_columns = tuple(
        ColumnDefinition(
            name=column_name,
            kind=column_kinds.get(column_name, TEXT_COLUMN_KIND),
            nullable=column_name not in primary_key_columns and column_name not in not_null_columns,
            unique=column_name in unique_columns,
        )
        for column_name in selected_columns
    )
    return data_columns


def _extract_user_name(*, last_modified_by: str | None, creator: str | None) -> str:
    for candidate in (last_modified_by, creator):
        if candidate is not None and candidate.strip():
            return candidate.strip()

    raise FileMetadataMissingError


def normalize_text_value(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    normalized = re.sub(r"\s+", " ", str(value)).strip()
    if not normalized or normalized == "-":
        return None
    return normalized


def convert_excel_value(  # noqa: PLR0913
    value: object,
    *,
    kind: ColumnKind,
    cell_reference: str,
    excel_errors_as_null: bool = False,
    source_name: str | None = None,
    preserve_text: bool = False,
    nullable: bool = True,
    excel_error_warnings: ExcelErrorWarnings | None = None,
) -> DbValue:
    excel_error_result = _excel_error_as_null(
        value,
        enabled=excel_errors_as_null,
        source_name=source_name,
        cell_reference=cell_reference,
        warnings_collector=excel_error_warnings,
    )
    if excel_error_result:
        return None
    if preserve_text and kind == "text":
        return _preserve_text_value(value)
    if kind == "bool":
        return _convert_boolean_value(value, nullable=nullable)
    normalized = normalize_text_value(value)
    if normalized is None:
        return None
    return _convert_normalized_value(
        value,
        normalized=normalized,
        kind=kind,
        cell_reference=cell_reference,
        source_name=source_name,
    )


def _convert_normalized_value(
    value: object,
    *,
    normalized: str,
    kind: ColumnKind,
    cell_reference: str,
    source_name: str | None = None,
) -> DbValue:
    if kind == "text":
        return normalized

    try:
        if kind == "int":
            numeric_value = float(normalized.replace(" ", "").replace(",", "."))
            if not numeric_value.is_integer():
                _raise_non_integer_value()
            return int(numeric_value)
        if kind == "float":
            return float(normalized.replace(" ", "").replace(",", "."))
        converted_datetime = _convert_datetime_value(value, normalized=normalized)
        if kind == "date":
            return converted_datetime.date()
        result = converted_datetime
    except (TypeError, ValueError, OverflowError) as error:
        raise CellTypeConversionError(cell_reference, kind, value, source_name=source_name) from error
    else:
        return result


def _convert_boolean_value(value: object, *, nullable: bool) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, Number):
        return value != 0
    if value is None or pd.isna(value) or (isinstance(value, str) and not value.strip()):
        return None if nullable else False

    normalized = str(value).strip().lower()
    if normalized in {"y", "yes", "да"}:
        return True
    if normalized in {"n", "no", "нет"}:
        return False
    return normalized != "false"


def _excel_error_as_null(
    value: object,
    *,
    enabled: bool,
    source_name: str | None,
    cell_reference: str,
    warnings_collector: ExcelErrorWarnings | None = None,
) -> bool:
    if not enabled or not _is_excel_error(value):
        return False
    if warnings_collector is not None:
        warnings_collector.record(value, cell_reference)
    else:
        LOGGER.warning(
            "Excel-файл %s: ошибочное значение %r в ячейке %s заменено на NULL.",
            source_name or "<неизвестный источник>",
            value,
            cell_reference,
        )
    return True


def _is_excel_error(value: object) -> bool:
    return isinstance(value, str) and value.strip().upper() in EXCEL_ERROR_VALUES


def _preserve_text_value(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    return value if isinstance(value, str) else str(value)


def _convert_datetime_value(value: object, *, normalized: str) -> datetime:
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, int | float):
        converted = pd.Timestamp(1899, 12, 30) + pd.to_timedelta(value, unit="D")
        if isinstance(converted, pd.Timestamp):
            return converted.to_pydatetime()
        raise ValueError

    converted_value = pd.to_datetime(normalized, dayfirst=True, errors="raise")
    if isinstance(converted_value, pd.Timestamp):
        return converted_value.to_pydatetime()
    raise ValueError


def excel_column_name(index: int) -> str:
    return _excel_column_name(index)


def _excel_column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _rename_locations(
    cell_locations: Sequence[Mapping[str, str]] | None,
    *,
    column_aliases: Mapping[str, str],
) -> Sequence[Mapping[str, str]] | None:
    if cell_locations is None:
        return None
    return [
        {column_aliases.get(column_name, column_name): location for column_name, location in row.items()}
        for row in cell_locations
    ]


def _rename_frame_origin(frame: pd.DataFrame, *, column_aliases: Mapping[str, str]) -> pd.DataFrame:
    origin = frame_origin(frame)
    if origin is None:
        return frame
    return attach_frame_origin(
        frame,
        sheet_name=origin.sheet_name,
        header_row=origin.header_row,
        column_letters={
            column_aliases.get(column_name, column_name): letter
            for column_name, letter in origin.column_letters.items()
        },
    )


def _cell_reference(
    origin: FrameOrigin | None,
    *,
    row_locations: Mapping[str, str] | None,
    column_name: str,
    row_position: int,
    fallback: str,
) -> str:
    if row_locations is not None:
        location = row_locations.get(column_name)
        if location is not None:
            return location
    if origin is None:
        return fallback
    cell = origin.cell_reference(column_name, row_position)
    if cell is None:
        return fallback
    return f"{origin.sheet_name}!{cell}"


def _row_reference(
    origin: FrameOrigin | None,
    *,
    row_locations: Mapping[str, str] | None,
    row_number: int,
) -> str | None:
    if row_locations is not None:
        for location in row_locations.values():
            sheet, _, cell = location.rpartition("!")
            digits = "".join(char for char in cell if char.isdigit())
            return f"{sheet}!{digits}" if sheet else digits
        return None
    if origin is None:
        return None
    return f"{origin.sheet_name}!{origin.header_row + 1 + (row_number - 2)}"


def _raise_non_integer_value() -> None:
    raise ValueError


def _is_check_column(column_name: str) -> bool:
    normalized_name = column_name.strip().lower()
    return normalized_name.startswith("check")
