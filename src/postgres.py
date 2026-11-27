import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Table, insert, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from business_errors import DatabaseOperationError, DuplicatePrimaryKeyError
from schema_definition import primary_key_columns, qualified_table_name, quote_identifier, split_table_reference
from stage_metrics import observe_stage
from storage import (
    POSTGRES_ROLE_EXISTS_SQL,
    align_table_structure,
    grant_select_on_table,
    grant_usage_on_schema,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from dictionary_models import DbRow, SourceHistoryEntry


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


async def replace_table_rows(  # noqa: PLR0913
    engine: AsyncEngine,
    table: Table,
    *,
    rows: Sequence[DbRow],
    history_table: Table | None = None,
    history_entries: Sequence[SourceHistoryEntry] = (),
    stage_module: str | None = None,
) -> int:
    stage_started_at = time.monotonic()
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {qualified_table_name(table)}"))
            await align_table_structure(connection, table)

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
    except IntegrityError as error:
        primary_key_constraint = table.primary_key.name
        if isinstance(primary_key_constraint, str) and primary_key_constraint in str(error):
            raise DuplicatePrimaryKeyError(primary_key_columns(table)) from error
        raise DatabaseOperationError.from_dbapi_error(
            error,
            operation="загрузка данных",
            table_reference=f"{table.schema}.{table.name}",
        ) from error
    except DBAPIError as error:
        raise DatabaseOperationError.from_dbapi_error(
            error,
            operation="изменение структуры или загрузка данных",
            table_reference=f"{table.schema}.{table.name}",
        ) from error

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
    async with engine.begin() as connection:
        granted_roles, skipped_roles = await grant_select_on_table(connection, table, role_names)
    return granted_roles, skipped_roles


async def grant_usage_on_schema_to_existing_roles(
    engine: AsyncEngine,
    *,
    schema_name: str,
    role_names: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    async with engine.begin() as connection:
        granted_roles, skipped_roles = await grant_usage_on_schema(connection, schema_name, role_names)
    return granted_roles, skipped_roles


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
            role_exists = bool(await connection.scalar(text(POSTGRES_ROLE_EXISTS_SQL), {"role_name": role_name}))
            if role_exists:
                await connection.execute(text(f"GRANT SELECT ON {quoted_relation} TO {quote_identifier(role_name)}"))
                granted_roles.append(role_name)
            else:
                skipped_roles.append(role_name)

    return tuple(granted_roles), tuple(skipped_roles)
