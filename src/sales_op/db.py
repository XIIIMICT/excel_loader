import time
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import TIMESTAMP, Column, Table, insert, text
from sqlalchemy.exc import DBAPIError

from business_errors import DatabaseOperationError
from sales_op.specification import PLAN_DATE_COLUMN_NAME, source_table_name
from schema_definition import build_dictionary_table, qualified_table_name, quote_identifier
from stage_metrics import observe_stage
from storage import ensure_additive_table_structure

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from config import Settings
    from dictionary_models import ColumnDefinition, DbRow, DbValue, SourceHistoryEntry
    from sales_op.models import SalesOpColumnMapping, SalesOpSpecification

VIEW_NAME = "sales_op_raw_view"
CREATED_AT_COLUMN_NAME = "created_at"
CREATED_AT_COMMENT = "Дата и время загрузки строки."
MONTHS_IN_YEAR = 12


def _quoted_reference(settings: Settings, name: str) -> str:
    """Quoted `<schema>.<name>` reference; used for SQL DDL."""
    return f"{quote_identifier(settings.modules['sales_op'].schema_name)}.{quote_identifier(name)}"


def build_sales_op_table(
    *,
    table_reference: str,
    columns: tuple[ColumnDefinition, ...],
) -> Table:
    """Physical table of one source: the spec columns plus the load timestamp."""
    table = build_dictionary_table(
        table_reference=table_reference,
        columns=columns,
        primary_key_columns=(),
    )
    table.append_column(_created_at_column())
    return table


def _created_at_column() -> Column[Any]:
    return Column(
        CREATED_AT_COLUMN_NAME,
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        info={
            "cast_expression": f"{quote_identifier(CREATED_AT_COLUMN_NAME)}::TIMESTAMPTZ",
            "ddl_type": "TIMESTAMPTZ",
            "information_schema_type": "timestamp with time zone",
        },
    )


async def replace_month_rows(  # noqa: PLR0913
    engine: AsyncEngine,
    table: Table,
    *,
    month_start: date,
    rows: Sequence[DbRow],
    history_table: Table | None = None,
    history_entries: Sequence[SourceHistoryEntry] = (),
    stage_module: str | None = None,
) -> int:
    """
    Replace the rows of one month in a single transaction.

    The DELETE is bounded by ``[month_start, next_month_start)`` so rows of
    other months survive; ``created_at`` comes from the column default. The
    table is created when missing and only missing columns are added — an
    existing table is never truncated, dropped or rewritten structurally.
    """
    next_month_start = _next_month_start(month_start)
    stage_started_at = time.monotonic()
    try:
        async with engine.begin() as connection:
            await ensure_additive_table_structure(connection, table)
            await connection.execute(
                text(
                    f"DELETE FROM {qualified_table_name(table)} "  # noqa: S608  # quoted internal identifier
                    f"WHERE {quote_identifier(PLAN_DATE_COLUMN_NAME)} >= :month_start "
                    f"AND {quote_identifier(PLAN_DATE_COLUMN_NAME)} < :next_month_start",
                ),
                {"month_start": month_start, "next_month_start": next_month_start},
            )
            if rows:
                await connection.execute(insert(table), list(rows))
            if history_table is not None and history_entries:
                await connection.execute(
                    insert(history_table),
                    [
                        {
                            "loaded_at": entry.loaded_at,
                            "src_file": entry.src_file,
                            "dest_table": entry.dest_table,
                            "file_modified_at": entry.file_modified_at,
                            "file_modifier": entry.file_modifier,
                        }
                        for entry in history_entries
                    ],
                )
            if stage_module is not None:
                observe_stage(stage_module, "load", time.monotonic() - stage_started_at)
        if stage_module is not None:
            observe_stage(stage_module, "commit", time.monotonic() - stage_started_at)
    except DBAPIError as error:
        raise DatabaseOperationError.from_dbapi_error(
            error,
            operation="замена данных за месяц",
            table_reference=f"{table.schema}.{table.name}",
        ) from error
    return len(rows)


def _next_month_start(month_start: date) -> date:
    if month_start.month == MONTHS_IN_YEAR:
        return date(month_start.year + 1, 1, 1)
    return date(month_start.year, month_start.month + 1, 1)


async def create_sales_op_view(
    settings: Settings,
    *,
    engine: AsyncEngine,
    specification: SalesOpSpecification,
) -> str:
    available_sources = await _available_sources(settings, engine=engine, specification=specification)
    if not available_sources:
        message = "нет таблиц sales_op для формирования представления"
        raise ValueError(message)

    view_reference = _quoted_reference(settings, VIEW_NAME)
    selects = [_source_select(settings, specification, source_code=source_code) for source_code in available_sources]
    async with engine.begin() as connection:
        await connection.execute(text(f"CREATE OR REPLACE VIEW {view_reference} AS\n" + "\nUNION ALL\n".join(selects)))
        for column_name, comment in _view_comments(specification):
            await connection.execute(
                text(
                    f"COMMENT ON COLUMN {view_reference}.{quote_identifier(column_name)} IS {_sql_literal(comment)}",
                ),
            )
    return settings.modules["sales_op"].table_reference(VIEW_NAME)


def _view_comments(specification: SalesOpSpecification) -> tuple[tuple[str, str], ...]:
    return (
        *((column.name, specification.descriptions[column.name]) for column in specification.columns),
        (CREATED_AT_COLUMN_NAME, CREATED_AT_COMMENT),
    )


async def _available_sources(
    settings: Settings,
    *,
    engine: AsyncEngine,
    specification: SalesOpSpecification,
) -> tuple[str, ...]:
    available: list[str] = []
    async with engine.connect() as connection:
        for source_code in specification.sources:
            exists = bool(
                await connection.scalar(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM information_schema.tables
                            WHERE table_schema = :schema_name AND table_name = :table_name
                        )
                        """,
                    ),
                    {
                        "schema_name": settings.modules["sales_op"].schema_name,
                        "table_name": source_table_name(source_code),
                    },
                ),
            )
            if exists:
                available.append(source_code)
    return tuple(available)


def _source_select(settings: Settings, specification: SalesOpSpecification, *, source_code: str) -> str:
    source_specification = specification.sources[source_code]
    table_reference = _quoted_reference(settings, source_table_name(source_code))
    projection_items = [
        *(_mapping_expression(mapping) for mapping in source_specification.mappings),
        f"{quote_identifier(CREATED_AT_COLUMN_NAME)} AS {quote_identifier(CREATED_AT_COLUMN_NAME)}",
    ]
    projection = ",\n    ".join(projection_items)
    return f"SELECT\n    {projection}\nFROM {table_reference}"


def _mapping_expression(mapping: SalesOpColumnMapping) -> str:
    target = quote_identifier(mapping.column.name)
    if mapping.mode in {"source", "plan_date"}:
        return f"{quote_identifier(mapping.column.name)} AS {target}"
    if mapping.mode == "null":
        return f"NULL::{_sql_type(mapping.column)} AS {target}"
    return f"{_typed_literal(mapping.constant, mapping.column)} AS {target}"


def _typed_literal(value: DbValue, column: ColumnDefinition) -> str:
    if value is None:
        return f"NULL::{_sql_type(column)}"
    if column.kind in {"float", "int"}:
        return str(value)
    return f"{_sql_literal(str(value))}::{_sql_type(column)}"


def _sql_type(column: ColumnDefinition) -> str:
    return {
        "date": "DATE",
        "datetime": "TIMESTAMP",
        "float": "DOUBLE PRECISION",
        "int": "INTEGER",
        "text": "TEXT",
        "timestamp_tz": "TIMESTAMPTZ",
    }[column.kind]


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
