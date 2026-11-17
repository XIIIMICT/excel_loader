import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import date, datetime
    from pathlib import Path

    from dictionary_models import ColumnDefinition, DbRow, DbValue, WorkbookMetadata

type MappingMode = Literal["constant", "null", "plan_date", "source"]


class SalesOpError(Exception):
    pass


class SalesOpMissingColumnsError(SalesOpError):
    pass


def sales_op_error(message: str) -> SalesOpError:
    return SalesOpError(message)


@dataclass(frozen=True, slots=True)
class SalesOpColumnMapping:
    column: ColumnDefinition
    description: str
    mode: MappingMode
    source_name: str | None = None
    constant: DbValue = None
    required: bool = False


@dataclass(frozen=True, slots=True)
class SalesOpSourceSpecification:
    code: str
    file_pattern: str
    mappings: tuple[SalesOpColumnMapping, ...]

    @property
    def table_columns(self) -> tuple[ColumnDefinition, ...]:
        return tuple(mapping.column for mapping in self.mappings if mapping.mode in {"source", "plan_date"})

    @property
    def required_columns(self) -> tuple[ColumnDefinition, ...]:
        return tuple(
            mapping.column for mapping in self.mappings if mapping.required and mapping.mode in {"source", "plan_date"}
        )

    def matches_file_name(self, file_name: str) -> bool:
        return re.fullmatch(self.file_pattern, file_name, flags=re.IGNORECASE) is not None


@dataclass(frozen=True, slots=True)
class SalesOpSpecification:
    columns: tuple[ColumnDefinition, ...]
    descriptions: dict[str, str]
    sources: dict[str, SalesOpSourceSpecification]


@dataclass(frozen=True, slots=True)
class SalesOpRunOptions:
    """
    CLI run options for the month-folder loader.

    ``months is None`` selects the current month from the application
    timezone; ``all_months`` selects every ``YYYY-MM`` folder under
    ``data_dir``. ``force`` includes files whose name starts with ``_``.
    """

    force: bool = False
    months: tuple[date, ...] | None = None
    all_months: bool = False


@dataclass(frozen=True, slots=True)
class SelectedSalesOpFile:
    source_code: str
    path: Path
    display_name: str
    original_name: str
    plan_date: date
    modified_at: datetime
    metadata: WorkbookMetadata
    history_reference: str


@dataclass(frozen=True, slots=True)
class ParsedSalesOpFile:
    plan_date: date
    modified_at: datetime
    sheet_name: str
    rows: list[DbRow]
    warning_count: int = 0
