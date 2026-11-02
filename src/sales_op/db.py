from typing import TYPE_CHECKING

from sqlalchemy import text

from sales_op.specification import source_table_name
from schema_definition import quote_identifier

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from config import Settings
    from dictionary_models import ColumnDefinition, DbValue
    from sales_op.models import SalesOpColumnMapping, SalesOpSpecification

VIEW_NAME = "sales_op_raw_view"


def _quoted_reference(settings: Settings, name: str) -> str:
    """Quoted `<schema>.<name>` reference; used for SQL DDL."""
    return f"{quote_identifier(settings.modules['sales_op'].schema_name)}.{quote_identifier(name)}"


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
        for column in specification.columns:
            comment = specification.descriptions[column.name]
            await connection.execute(
                text(
                    f"COMMENT ON COLUMN {view_reference}.{quote_identifier(column.name)} IS {_sql_literal(comment)}",
                ),
            )
    return settings.modules["sales_op"].table_reference(VIEW_NAME)


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
    projection_items = [*(_mapping_expression(mapping) for mapping in source_specification.mappings)]
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
