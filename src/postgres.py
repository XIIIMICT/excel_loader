from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Table, insert, text
from sqlalchemy.exc import IntegrityError

from business_errors import DuplicatePrimaryKeyError
from schema_definition import primary_key_columns, qualified_table_name, quote_identifier, split_table_reference
from storage import align_table_structure

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from dictionary_models import DbRow


@dataclass(frozen=True, slots=True)
class StorageState:
    database_exists: bool
    schema_exists: bool
    table_exists: bool

    @property
    def needs_seed(self) -> bool:
        return not (self.database_exists and self.schema_exists and self.table_exists)


async def inspect_storage(
    table: Table,
    *,
    engine: AsyncEngine,
) -> StorageState:
    async with engine.connect() as connection:
        schema_exists = bool(
            await connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema_name)"),
                {"schema_name": table.schema},
            ),
        )
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

    return StorageState(
        database_exists=True,
        schema_exists=schema_exists,
        table_exists=table_exists,
    )


async def replace_table_rows(
    engine: AsyncEngine,
    table: Table,
    *,
    rows: Sequence[DbRow],
) -> int:
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {qualified_table_name(table)}"))
            await align_table_structure(connection, table)

            if rows:
                await connection.execute(insert(table), list(rows))
    except IntegrityError as error:
        primary_key_constraint = table.primary_key.name
        if isinstance(primary_key_constraint, str) and primary_key_constraint in str(error):
            raise DuplicatePrimaryKeyError(primary_key_columns(table)) from error

        raise

    return len(rows)


async def refresh_existing_materialized_views(
    engine: AsyncEngine,
    *,
    view_references: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    refreshed_views: list[str] = []
    skipped_views: list[str] = []
    async with engine.begin() as connection:
        for view_reference in view_references:
            schema_name, view_name = split_table_reference(view_reference)
            view_exists = bool(
                await connection.scalar(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM pg_matviews
                            WHERE schemaname = :schema_name
                              AND matviewname = :view_name
                        )
                        """,
                    ),
                    {"schema_name": schema_name, "view_name": view_name},
                ),
            )
            normalized_view_name = f"{schema_name}.{view_name}"
            if not view_exists:
                skipped_views.append(normalized_view_name)
                continue

            await connection.execute(
                text(
                    f"REFRESH MATERIALIZED VIEW {quote_identifier(schema_name)}.{quote_identifier(view_name)}",
                ),
            )
            refreshed_views.append(normalized_view_name)

    return tuple(refreshed_views), tuple(skipped_views)


async def grant_select_on_table_to_existing_roles(
    engine: AsyncEngine,
    *,
    table: Table,
    role_names: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    granted_roles: list[str] = []
    skipped_roles: list[str] = []
    async with engine.begin() as connection:
        for role_name in role_names:
            role_exists = bool(
                await connection.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
                    {"role_name": role_name},
                ),
            )
            if not role_exists:
                skipped_roles.append(role_name)
                continue

            await connection.execute(
                text(
                    f"GRANT SELECT ON TABLE {qualified_table_name(table)} TO {quote_identifier(role_name)}",
                ),
            )
            granted_roles.append(role_name)

    return tuple(granted_roles), tuple(skipped_roles)


async def grant_usage_on_schema_to_existing_roles(
    engine: AsyncEngine,
    *,
    schema_name: str,
    role_names: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    granted_roles: list[str] = []
    skipped_roles: list[str] = []
    async with engine.begin() as connection:
        for role_name in role_names:
            role_exists = bool(
                await connection.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
                    {"role_name": role_name},
                ),
            )
            if not role_exists:
                skipped_roles.append(role_name)
                continue

            await connection.execute(
                text(
                    f"GRANT USAGE ON SCHEMA {quote_identifier(schema_name)} TO {quote_identifier(role_name)}",
                ),
            )
            granted_roles.append(role_name)

    return tuple(granted_roles), tuple(skipped_roles)


async def grant_select_on_relation_to_existing_roles(
    engine: AsyncEngine,
    *,
    relation_reference: str,
    role_names: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    schema_name, relation_name = split_table_reference(relation_reference)
    quoted_relation = f"{quote_identifier(schema_name)}.{quote_identifier(relation_name)}"
    granted_roles: list[str] = []
    skipped_roles: list[str] = []
    async with engine.begin() as connection:
        for role_name in role_names:
            role_exists = bool(
                await connection.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
                    {"role_name": role_name},
                ),
            )
            if not role_exists:
                skipped_roles.append(role_name)
                continue

            await connection.execute(text(f"GRANT SELECT ON {quoted_relation} TO {quote_identifier(role_name)}"))
            granted_roles.append(role_name)

    return tuple(granted_roles), tuple(skipped_roles)
