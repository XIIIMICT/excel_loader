from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

type ColumnKind = Literal["bool", "date", "datetime", "float", "int", "text", "timestamp_tz"]
type DbValue = str | bool | int | float | date | datetime | None
type DbRow = dict[str, DbValue]


@dataclass(frozen=True, slots=True)
class WorkbookMetadata:
    file_modifier: str
    file_modified_at: datetime
    loaded_at: datetime


@dataclass(frozen=True, slots=True)
class ColumnDefinition:
    name: str
    kind: ColumnKind
    nullable: bool
    unique: bool = False


@dataclass(frozen=True, slots=True)
class DictionaryPayload:
    table_reference: str
    primary_key_columns: tuple[str, ...]
    columns: tuple[ColumnDefinition, ...]
    rows: list[DbRow]
    metadata: WorkbookMetadata
    warning_count: int = 0


@dataclass(frozen=True, slots=True)
class CombinedDictionaryPayload:
    payload: DictionaryPayload
    source_metadata: tuple[WorkbookMetadata, ...]


@dataclass(frozen=True, slots=True)
class SourceHistoryEntry:
    loaded_at: datetime
    src_file: str
    dest_table: str
    file_modified_at: datetime
    file_modifier: str
