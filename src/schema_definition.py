from typing import TYPE_CHECKING, Any

from sqlalchemy import TIMESTAMP, Column, Date, DateTime, Float, Integer, MetaData, PrimaryKeyConstraint, Table, Text

if TYPE_CHECKING:
    from dictionary_models import ColumnDefinition


def build_dictionary_table(
    *,
    table_reference: str,
    columns: tuple[ColumnDefinition, ...],
    primary_key_columns: tuple[str, ...],
) -> Table:
    schema_name, table_name = split_table_reference(table_reference)
    metadata = MetaData(schema=schema_name)
    constraints = (PrimaryKeyConstraint(*primary_key_columns),) if primary_key_columns else ()
    return Table(table_name, metadata, *(_build_column(column) for column in columns), *constraints)


def split_table_reference(table_reference: str) -> tuple[str, str]:
    parts = [part.strip() for part in table_reference.split(".")]
    if len(parts) == 1:
        return "public", parts[0]

    schema_name, table_name = parts
    return schema_name, table_name


def primary_key_columns(table: Table) -> tuple[str, ...]:
    return tuple(column.name for column in table.primary_key.columns)


def quote_identifier(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def qualified_table_name(table: Table) -> str:
    schema_name = table.schema or "public"
    return f"{quote_identifier(schema_name)}.{quote_identifier(table.name)}"


def column_ddl_type(column: Column[Any]) -> str:
    return str(column.info["ddl_type"])


def column_information_schema_type(column: Column[Any]) -> str:
    return str(column.info["information_schema_type"])


def column_cast_expression(column: Column[Any]) -> str:
    cast_expression = column.info.get("cast_expression")
    if cast_expression is not None:
        return str(cast_expression)

    return f"{quote_identifier(column.name)}::{column_ddl_type(column)}"


def _build_column(column: ColumnDefinition) -> Column[Any]:
    if column.kind == "text":
        return _text_column(column.name, nullable=column.nullable)
    if column.kind == "date":
        return _date_column(column.name, nullable=column.nullable)
    if column.kind == "datetime":
        return _datetime_column(column.name, nullable=column.nullable)
    if column.kind == "float":
        return _float_column(column.name, nullable=column.nullable)
    if column.kind == "int":
        return _integer_column(column.name, nullable=column.nullable)

    return _timestamp_tz_column(column.name, nullable=column.nullable)


def _text_column(name: str, *, nullable: bool) -> Column[Any]:
    return Column(
        name,
        Text,
        nullable=nullable,
        info={
            "cast_expression": f"{quote_identifier(name)}::TEXT",
            "ddl_type": "TEXT",
            "information_schema_type": "text",
        },
    )


def _date_column(name: str, *, nullable: bool) -> Column[Any]:
    return Column(
        name,
        Date,
        nullable=nullable,
        info={
            "cast_expression": f"{quote_identifier(name)}::DATE",
            "ddl_type": "DATE",
            "information_schema_type": "date",
        },
    )


def _timestamp_tz_column(name: str, *, nullable: bool) -> Column[Any]:
    return Column(
        name,
        TIMESTAMP(timezone=True),
        nullable=nullable,
        info={
            "cast_expression": f"{quote_identifier(name)}::TIMESTAMPTZ",
            "ddl_type": "TIMESTAMPTZ",
            "information_schema_type": "timestamp with time zone",
        },
    )


def _datetime_column(name: str, *, nullable: bool) -> Column[Any]:
    return Column(
        name,
        DateTime,
        nullable=nullable,
        info={
            "cast_expression": f"{quote_identifier(name)}::TIMESTAMP",
            "ddl_type": "TIMESTAMP",
            "information_schema_type": "timestamp without time zone",
        },
    )


def _float_column(name: str, *, nullable: bool) -> Column[Any]:
    return Column(
        name,
        Float,
        nullable=nullable,
        info={
            "cast_expression": f"{quote_identifier(name)}::DOUBLE PRECISION",
            "ddl_type": "DOUBLE PRECISION",
            "information_schema_type": "double precision",
        },
    )


def _integer_column(name: str, *, nullable: bool) -> Column[Any]:
    return Column(
        name,
        Integer,
        nullable=nullable,
        info={
            "cast_expression": f"{quote_identifier(name)}::INTEGER",
            "ddl_type": "INTEGER",
            "information_schema_type": "integer",
        },
    )
