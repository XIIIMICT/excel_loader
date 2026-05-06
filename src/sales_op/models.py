from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import date, datetime
    from pathlib import Path

    from dictionary_models import ColumnDefinition, DbRow, DbValue

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


@dataclass(frozen=True, slots=True)
class SalesOpSourceSpecification:
    code: str
    mappings: tuple[SalesOpColumnMapping, ...]

    @property
    def table_columns(self) -> tuple[ColumnDefinition, ...]:
        return tuple(mapping.column for mapping in self.mappings if mapping.mode in {"source", "plan_date"})


@dataclass(frozen=True, slots=True)
class SalesOpSpecification:
    columns: tuple[ColumnDefinition, ...]
    descriptions: dict[str, str]
    sources: dict[str, SalesOpSourceSpecification]


@dataclass(frozen=True, slots=True)
class SelectedSalesOpFile:
    source_code: str
    path: Path
    display_name: str
    original_name: str
    plan_date: date
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class ParsedSalesOpFile:
    plan_date: date
    modified_at: datetime
    sheet_name: str
    rows: list[DbRow]
    warning_count: int = 0
