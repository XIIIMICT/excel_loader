from typing import TYPE_CHECKING

from sqlalchemy import text

from sales_op.registry import SOURCE_DEFINITIONS
from schema_definition import quote_identifier

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from config import Settings
    from dictionary_models import ColumnDefinition, DbValue
    from sales_op.models import SalesOpColumnMapping, SalesOpSpecification

VIEW_NAME = "sales_op_raw_view"


async def create_sales_op_view(
    settings: Settings,
    *,
    engine: AsyncEngine,
    specification: SalesOpSpecification,
) -> str:
    available_sources = await _available_sources(settings, engine=engine)
    if not available_sources:
        message = "нет таблиц sales_op для формирования представления"
        raise ValueError(message)

    view_reference = f"{quote_identifier(settings.sales_op_schema)}.{quote_identifier(VIEW_NAME)}"
    selects = [_source_select(settings, specification, source_code=source_code) for source_code in available_sources]
    async with engine.begin() as connection:
        await connection.execute(text(f"DROP VIEW IF EXISTS {view_reference}"))
        await connection.execute(text(f"CREATE VIEW {view_reference} AS\n" + "\nUNION ALL\n".join(selects)))
        for column in specification.columns:
            comment = specification.descriptions[column.name]
            await connection.execute(
                text(
                    f"COMMENT ON COLUMN {view_reference}.{quote_identifier(column.name)} IS {_sql_literal(comment)}",
                ),
            )
    return f"{settings.sales_op_schema}.{VIEW_NAME}"


async def _available_sources(settings: Settings, *, engine: AsyncEngine) -> tuple[str, ...]:
    available: list[str] = []
    async with engine.connect() as connection:
        for definition in SOURCE_DEFINITIONS:
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
                    {"schema_name": settings.sales_op_schema, "table_name": definition.table_name},
                ),
            )
            if exists:
                available.append(definition.code)
    return tuple(available)


def _source_select(settings: Settings, specification: SalesOpSpecification, *, source_code: str) -> str:
    source_specification = specification.sources[source_code]
    definition = next(item for item in SOURCE_DEFINITIONS if item.code == source_code)
    table_reference = f"{quote_identifier(settings.sales_op_schema)}.{quote_identifier(definition.table_name)}"
    projection = ",\n    ".join(_mapping_expression(mapping) for mapping in source_specification.mappings)
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
