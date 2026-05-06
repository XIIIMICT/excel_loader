from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

type ColumnKind = Literal["date", "datetime", "float", "int", "text", "timestamp_tz"]
type DbValue = str | int | float | date | datetime | None
type DbRow = dict[str, DbValue]


@dataclass(frozen=True, slots=True)
class WorkbookMetadata:
    user_name: str
    date_modified: datetime
    date_upload: datetime


@dataclass(frozen=True, slots=True)
class ColumnDefinition:
    name: str
    kind: ColumnKind
    nullable: bool


@dataclass(frozen=True, slots=True)
class DictionaryPayload:
    table_reference: str
    primary_key_columns: tuple[str, ...]
    columns: tuple[ColumnDefinition, ...]
    rows: list[DbRow]
    metadata: WorkbookMetadata
    warning_count: int = 0
