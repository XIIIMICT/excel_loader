from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, Table, text
from sqlalchemy.schema import CreateSchema

from schema_definition import (
    column_cast_expression,
    column_ddl_type,
    column_information_schema_type,
    primary_key_columns,
    qualified_table_name,
    quote_identifier,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


async def prepare_storage(
    table: Table,
    *,
    engine: AsyncEngine,
    users_to_grant: tuple[str, ...] = (),
) -> None:
    async with engine.begin() as connection:
        await _ensure_schema_exists(connection, table)
        await _grant_usage_on_schema(connection, table.schema or "public", users_to_grant)
        await _ensure_table_exists(connection, table)
        await _grant_select_on_table(connection, table, users_to_grant)


async def align_table_structure(connection: AsyncConnection, table: Table) -> None:
    existing_columns = await _fetch_existing_columns(connection, table)
    await _drop_retired_file_metadata_columns(connection, table, existing_columns)
    await _ensure_columns(connection, table, existing_columns)
    await _ensure_primary_key(connection, table)
    await _ensure_unique_constraints(connection, table)


async def _drop_retired_file_metadata_columns(
    connection: AsyncConnection,
    table: Table,
    existing_columns: dict[str, dict[str, Any]],
) -> None:
    expected_columns = {column.name for column in table.columns}
    for column_name in ("user_name", "date_modified"):
        if column_name not in existing_columns or column_name in expected_columns:
            continue
        await connection.execute(
            text(f"ALTER TABLE {qualified_table_name(table)} DROP COLUMN {quote_identifier(column_name)}"),
        )
        existing_columns.pop(column_name)


async def _ensure_schema_exists(connection: AsyncConnection, table: Table) -> None:
    schema_name = table.schema or "public"
    await connection.execute(CreateSchema(schema_name, if_not_exists=True))


async def _grant_usage_on_schema(
    connection: AsyncConnection,
    schema_name: str,
    role_names: tuple[str, ...],
) -> None:
    for role_name in role_names:
        role_exists = bool(
            await connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
                {"role_name": role_name},
            ),
        )
        if role_exists:
            await connection.execute(
                text(f"GRANT USAGE ON SCHEMA {quote_identifier(schema_name)} TO {quote_identifier(role_name)}"),
            )


async def _grant_select_on_table(
    connection: AsyncConnection,
    table: Table,
    role_names: tuple[str, ...],
) -> None:
    for role_name in role_names:
        role_exists = bool(
            await connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
                {"role_name": role_name},
            ),
        )
        if role_exists:
            await connection.execute(
                text(f"GRANT SELECT ON TABLE {qualified_table_name(table)} TO {quote_identifier(role_name)}"),
            )


async def _ensure_table_exists(connection: AsyncConnection, table: Table) -> None:
    table_exists = bool(
        await connection.scalar(
            text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = :schema_name
                      AND table_name = :table_name
                )
                """,
            ),
            {"schema_name": table.schema, "table_name": table.name},
        ),
    )
    await connection.run_sync(lambda sync_connection: table.metadata.create_all(sync_connection, tables=[table]))
    if table_exists:
        return


async def _ensure_primary_key(connection: AsyncConnection, table: Table) -> None:
    result = await connection.execute(
        text(
            """
            SELECT
                pk_constraint.conname AS constraint_name,
                ARRAY_AGG(attribute.attname ORDER BY key_columns.ordinality) AS column_names
            FROM pg_constraint AS pk_constraint
            JOIN pg_class AS relation ON relation.oid = pk_constraint.conrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            JOIN unnest(pk_constraint.conkey) WITH ORDINALITY AS key_columns(attnum, ordinality) ON TRUE
            JOIN pg_attribute AS attribute
                ON attribute.attrelid = relation.oid
               AND attribute.attnum = key_columns.attnum
            WHERE pk_constraint.contype = 'p'
              AND namespace.nspname = :schema_name
              AND relation.relname = :table_name
            GROUP BY pk_constraint.conname
            """,
        ),
        {"schema_name": table.schema, "table_name": table.name},
    )
    primary_key_row = result.mappings().one_or_none()
    expected_primary_key_columns = primary_key_columns(table)
    if not expected_primary_key_columns:
        if primary_key_row is None:
            return
        msg = f"У таблицы {table.schema}.{table.name} есть первичный ключ, хотя он не ожидался."
        raise ValueError(msg)

    quoted_primary_key_columns = ", ".join(
        quote_identifier(column_name) for column_name in expected_primary_key_columns
    )
    if primary_key_row is None:
        await connection.execute(
            text(
                f"""
                ALTER TABLE {qualified_table_name(table)}
                ADD PRIMARY KEY ({quoted_primary_key_columns})
                """,
            ),
        )
        return

    current_primary_key_columns = tuple(primary_key_row["column_names"])
    if current_primary_key_columns != expected_primary_key_columns:
        msg = (
            f"У таблицы {table.schema}.{table.name} уже есть первичный ключ по {current_primary_key_columns!r}, "
            f"ожидался {expected_primary_key_columns!r}."
        )
        raise ValueError(msg)


async def _fetch_existing_columns(connection: AsyncConnection, table: Table) -> dict[str, dict[str, Any]]:
    result = await connection.execute(
        text(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = :schema_name
              AND table_name = :table_name
            """,
        ),
        {"schema_name": table.schema, "table_name": table.name},
    )
    return {
        str(row["column_name"]): {
            "data_type": str(row["data_type"]),
            "nullable": str(row["is_nullable"]) == "YES",
        }
        for row in result.mappings()
    }


async def _ensure_unique_constraints(connection: AsyncConnection, table: Table) -> None:
    expected_columns = {column.name for column in table.columns if column.unique}
    if not expected_columns:
        return

    result = await connection.execute(
        text(
            """
            SELECT attribute.attname AS column_name
            FROM pg_constraint AS unique_constraint
            JOIN pg_class AS relation ON relation.oid = unique_constraint.conrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            JOIN unnest(unique_constraint.conkey) AS key_column(attnum) ON TRUE
            JOIN pg_attribute AS attribute
              ON attribute.attrelid = relation.oid
             AND attribute.attnum = key_column.attnum
            WHERE unique_constraint.contype = 'u'
              AND cardinality(unique_constraint.conkey) = 1
              AND namespace.nspname = :schema_name
              AND relation.relname = :table_name
            """,
        ),
        {"schema_name": table.schema, "table_name": table.name},
    )
    existing_columns = {str(row["column_name"]) for row in result.mappings()}
    for column_name in sorted(expected_columns - existing_columns):
        constraint_name = f"uq_{table.name}_{column_name}"
        await connection.execute(
            text(
                f"ALTER TABLE {qualified_table_name(table)} "
                f"ADD CONSTRAINT {quote_identifier(constraint_name)} UNIQUE ({quote_identifier(column_name)})",
            ),
        )


async def _ensure_columns(
    connection: AsyncConnection,
    table: Table,
    existing_columns: Mapping[str, Mapping[str, Any]],
) -> None:
    for column in table.columns:
        current_column = existing_columns.get(column.name)
        if current_column is None:
            await connection.execute(
                text(f"ALTER TABLE {qualified_table_name(table)} ADD COLUMN {_column_definition(column)}"),
            )
            current_column = {"data_type": None, "nullable": None}

        if current_column["data_type"] != column_information_schema_type(column):
            await connection.execute(
                text(
                    f"""
                    ALTER TABLE {qualified_table_name(table)}
                    ALTER COLUMN {quote_identifier(column.name)} TYPE {column_ddl_type(column)}
                    USING {column_cast_expression(column)}
                    """,
                ),
            )

        if current_column["nullable"] != column.nullable:
            nullability_sql = "DROP NOT NULL" if column.nullable else "SET NOT NULL"
            await connection.execute(
                text(
                    f"""
                    ALTER TABLE {qualified_table_name(table)}
                    ALTER COLUMN {quote_identifier(column.name)} {nullability_sql}
                    """,
                ),
            )


def _column_definition(column: Column[Any]) -> str:
    nullability = "" if column.nullable else " NOT NULL"
    return f"{quote_identifier(column.name)} {column_ddl_type(column)}{nullability}"
