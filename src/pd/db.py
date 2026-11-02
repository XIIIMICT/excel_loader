import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

# ruff: noqa: S608
from sqlalchemy import Table, delete, insert, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateSchema

from pd.schema import PdTables, build_pd_tables, pd_view_reference
from schema_definition import qualified_table_name, quote_identifier

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
    from datetime import datetime

    from sqlalchemy.engine import RowMapping
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

    from config import Settings
    from dictionary_models import SourceHistoryEntry
    from pd.models import Asset, Indicator, ParsedReport, RawMaterial


type Record = dict[str, object | None]
type ReferenceKey = str | tuple[str, ...] | tuple[str | None, str | None, str | None, str]
POSTGRES_IDENTIFIER_MAX_BYTES = 63
HASH_SUFFIX_LENGTH = 8


@dataclass(frozen=True, slots=True)
class PdReferenceChange:
    action: str
    entity_type: str
    entity_name: str
    details: str | None


@dataclass(frozen=True, slots=True)
class PdLoadResult:
    deleted_rows: int
    inserted_rows: int
    changes: tuple[PdReferenceChange, ...]
    view_name: str

    @property
    def has_format_changes(self) -> bool:
        return bool(self.changes)


@dataclass(frozen=True, slots=True)
class ViewBuildResult:
    view_name: str
    fallback_used: bool
    error_message: str | None


async def load_report_to_db(
    settings: Settings,
    engine: AsyncEngine,
    report: ParsedReport,
    *,
    history_table: Table | None = None,
    history_entry: SourceHistoryEntry | None = None,
) -> PdLoadResult:
    tables = build_pd_tables(settings)
    async with engine.begin() as connection:
        await _ensure_pd_storage(connection, tables, role_names=settings.modules["pd"].users_to_grant)
        reference_ids, changes = await _resolve_report_references(connection, tables, report)
        deleted_rows = await _replace_data_for_date(connection, tables.data, report, reference_ids=reference_ids)
        view_result = await create_or_replace_pd_view(settings, connection, tables)
        if view_result.fallback_used:
            changes.append(
                PdReferenceChange(
                    action="view_fallback",
                    entity_type="view",
                    entity_name=view_result.view_name,
                    details=view_result.error_message,
                ),
            )
        await _insert_change_log(connection, tables, changes, report=report)
        if history_table is not None and history_entry is not None:
            await connection.execute(
                insert(history_table).values(
                    loaded_at=history_entry.loaded_at,
                    src_file=history_entry.src_file,
                    dest_table=history_entry.dest_table,
                    file_modified_at=history_entry.file_modified_at,
                    file_modifier=history_entry.file_modifier,
                ),
            )
        await _grant_select_on_relation(
            connection,
            view_result.view_name,
            role_names=settings.modules["pd"].users_to_grant,
        )

    return PdLoadResult(
        deleted_rows=deleted_rows,
        inserted_rows=len(report.data),
        changes=tuple(changes),
        view_name=view_result.view_name,
    )


async def create_or_replace_pd_view(
    settings: Settings,
    connection: AsyncConnection,
    tables: PdTables,
) -> ViewBuildResult:
    indicators = await _fetch_indicators(connection, tables.indicators)
    module = settings.modules["pd"]
    base_view_name = "pd_view"

    async def query_for(view_name: str) -> str:
        return _pd_view_query(
            tables,
            indicators=indicators,
            view_reference=f"{quote_identifier(module.schema_name)}.{quote_identifier(view_name)}",
            replace=True,
        )

    async def fallback_query_for(view_name: str) -> str:
        return _pd_view_query(
            tables,
            indicators=indicators,
            view_reference=f"{quote_identifier(module.schema_name)}.{quote_identifier(view_name)}",
            replace=False,
        )

    view_result = await _create_or_replace_view_with_fallback(
        connection,
        schema_name=module.schema_name,
        base_view_name=base_view_name,
        query_for=query_for,
        fallback_query_for=fallback_query_for,
    )
    await _comment_pd_view_columns(connection, view_result.view_name, indicators=indicators)
    return view_result


async def _ensure_pd_storage(
    connection: AsyncConnection,
    tables: PdTables,
    *,
    role_names: Sequence[str],
) -> None:
    if tables.metadata.schema is not None:
        await connection.execute(CreateSchema(tables.metadata.schema, if_not_exists=True))
        await _grant_usage_on_schema(connection, tables.metadata.schema, role_names)
    await connection.run_sync(tables.metadata.create_all)
    await connection.execute(
        text(f"ALTER TABLE {qualified_table_name(tables.data)} ADD COLUMN IF NOT EXISTS value_double DOUBLE PRECISION"),
    )
    await _ensure_materials_columns(connection, tables.materials)
    for table in (tables.assets, tables.materials, tables.indicators, tables.data, tables.change_log):
        await _grant_select_on_table(connection, table, role_names)


async def _ensure_materials_columns(connection: AsyncConnection, table: Table) -> None:
    table_name = qualified_table_name(table)
    await connection.execute(
        text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS is_gross BOOLEAN NOT NULL DEFAULT false"),
    )
    await connection.execute(
        text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS is_commercial BOOLEAN NOT NULL DEFAULT false"),
    )
    await connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS product_grade TEXT"))
    await connection.execute(
        text(
            f"COMMENT ON COLUMN {table_name}.is_gross IS "
            "'Валовый признак. По умолчанию включается, если название содержит ''вал''.'",
        ),
    )
    await connection.execute(
        text(
            f"COMMENT ON COLUMN {table_name}.is_commercial IS "
            "'Товарный признак. По умолчанию включается, если название содержит ''товар'' или ''тов.''.'",
        ),
    )
    await connection.execute(
        text(
            f"COMMENT ON COLUMN {table_name}.product_grade IS 'Марка товара. По умолчанию NULL, заполняется вручную.'",
        ),
    )


async def _grant_usage_on_schema(
    connection: AsyncConnection,
    schema_name: str,
    role_names: Sequence[str],
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
    role_names: Sequence[str],
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


async def _resolve_report_references(
    connection: AsyncConnection,
    tables: PdTables,
    report: ParsedReport,
) -> tuple[dict[str, dict[ReferenceKey, UUID]], list[PdReferenceChange]]:
    changes: list[PdReferenceChange] = []
    asset_ids = await _resolve_assets(connection, tables.assets, report.assets, loaded_at=report.loaded_at)
    material_ids = await _resolve_materials(
        connection,
        tables.materials,
        report.raw_materials,
        loaded_at=report.loaded_at,
    )
    indicator_ids = await _resolve_indicators(
        connection,
        tables.indicators,
        report.indicators,
        loaded_at=report.loaded_at,
    )

    changes.extend(_created_changes("asset", asset_ids.created_names))
    changes.extend(_created_changes("material", material_ids.created_names))
    changes.extend(_created_changes("indicator", indicator_ids.created_names))
    return (
        {
            "assets": asset_ids.ids_by_name,
            "materials": material_ids.ids_by_name,
            "indicators": indicator_ids.ids_by_name,
        },
        changes,
    )


@dataclass(frozen=True, slots=True)
class ResolvedIds:
    ids_by_name: dict[ReferenceKey, UUID]
    created_names: tuple[str, ...]


async def _resolve_assets(
    connection: AsyncConnection,
    table: Table,
    assets: Sequence[Asset],
    *,
    loaded_at: datetime,
) -> ResolvedIds:
    ids_by_name = await _fetch_ids_by_name(connection, table, [asset.name for asset in assets])
    created_names: list[str] = []
    for asset in assets:
        if asset.name in ids_by_name:
            await connection.execute(
                update(table).where(table.c.id == ids_by_name[asset.name]).values(updated_at=loaded_at),
            )
            continue

        asset_id = uuid4()
        result = await connection.execute(
            insert(table)
            .values(id=asset_id, name=asset.name, created_at=loaded_at, updated_at=loaded_at)
            .returning(table.c.id),
        )
        ids_by_name[asset.name] = cast("UUID", result.scalar_one())
        created_names.append(asset.name)
    return ResolvedIds(ids_by_name=cast("dict[ReferenceKey, UUID]", ids_by_name), created_names=tuple(created_names))


async def _resolve_materials(
    connection: AsyncConnection,
    table: Table,
    raw_materials: Sequence[RawMaterial],
    *,
    loaded_at: datetime,
) -> ResolvedIds:
    material_keys = _material_keys(raw_materials)
    ids_by_name = await _fetch_material_ids(connection, table)
    created_names: list[str] = []
    for path in material_keys:
        name = path[-1]
        parent_path = path[:-1]
        parent_id = ids_by_name[parent_path] if parent_path else None
        key = path
        material_flags = _material_flags(name)
        if key in ids_by_name:
            await connection.execute(update(table).where(table.c.id == ids_by_name[key]).values(updated_at=loaded_at))
            continue

        material_id = uuid4()
        result = await connection.execute(
            insert(table)
            .values(
                id=material_id,
                name=name,
                parent_id=parent_id,
                created_at=loaded_at,
                updated_at=loaded_at,
                product_grade=None,
                **material_flags,
            )
            .returning(table.c.id),
        )
        ids_by_name[key] = cast("UUID", result.scalar_one())
        created_names.append(" | ".join(path))
    return ResolvedIds(ids_by_name=cast("dict[ReferenceKey, UUID]", ids_by_name), created_names=tuple(created_names))


def _material_flags(name: str) -> dict[str, bool]:
    normalized_name = name.casefold()
    return {
        "is_gross": "вал" in normalized_name,
        "is_commercial": "товар" in normalized_name or "тов." in normalized_name,
    }


def quote_sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def _resolve_indicators(
    connection: AsyncConnection,
    table: Table,
    indicators: Sequence[Indicator],
    *,
    loaded_at: datetime,
) -> ResolvedIds:
    ids_by_name = await _fetch_indicator_ids(connection, table)
    used_db_names = await _fetch_used_indicator_db_names(connection, table)
    created_names: list[str] = []
    for indicator in indicators:
        key = _indicator_key(indicator)
        values = {
            "category_1": indicator.category_1,
            "category_2": indicator.category_2,
            "category_3": indicator.category_3,
            "name": indicator.name,
            "ordinal_number": indicator.ordinal_number,
            "updated_at": loaded_at,
        }
        if key in ids_by_name:
            await connection.execute(update(table).where(table.c.id == ids_by_name[key]).values(**values))
            continue

        indicator_id = uuid4()
        db_name = _default_indicator_db_name(indicator.db_name or indicator.full_name, used_db_names)
        result = await connection.execute(
            insert(table)
            .values(
                id=indicator_id,
                db_name=db_name,
                created_at=loaded_at,
                **values,
            )
            .returning(table.c.id),
        )
        ids_by_name[key] = cast("UUID", result.scalar_one())
        used_db_names.add(db_name)
        created_names.append(indicator.full_name)
    return ResolvedIds(ids_by_name=cast("dict[ReferenceKey, UUID]", ids_by_name), created_names=tuple(created_names))


async def _fetch_ids_by_name(connection: AsyncConnection, table: Table, names: Iterable[str]) -> dict[str, UUID]:
    unique_names = sorted(set(names))
    if not unique_names:
        return {}

    result = await connection.execute(select(table.c.id, table.c.name).where(table.c.name.in_(unique_names)))
    return {str(row["name"]): cast("UUID", row["id"]) for row in result.mappings()}


async def _fetch_material_ids(connection: AsyncConnection, table: Table) -> dict[tuple[str, ...], UUID]:
    result = await connection.execute(select(table.c.id, table.c.name, table.c.parent_id))
    rows = list(result.mappings())
    names_by_id = {cast("UUID", row["id"]): str(row["name"]) for row in rows}
    parent_ids_by_id = {cast("UUID", row["id"]): cast("UUID | None", row["parent_id"]) for row in rows}
    return {
        _material_path(material_id, names_by_id=names_by_id, parent_ids_by_id=parent_ids_by_id): material_id
        for material_id in names_by_id
    }


async def _fetch_indicator_ids(
    connection: AsyncConnection,
    table: Table,
) -> dict[tuple[str | None, str | None, str | None, str], UUID]:
    result = await connection.execute(
        select(table.c.id, table.c.category_1, table.c.category_2, table.c.category_3, table.c.name),
    )
    return {
        (
            cast("str | None", row["category_1"]),
            cast("str | None", row["category_2"]),
            cast("str | None", row["category_3"]),
            str(row["name"]),
        ): cast("UUID", row["id"])
        for row in result.mappings()
    }


def _indicator_key(indicator: Indicator) -> tuple[str | None, str | None, str | None, str]:
    return (indicator.category_1, indicator.category_2, indicator.category_3, indicator.name)


async def _fetch_used_indicator_db_names(connection: AsyncConnection, table: Table) -> set[str]:
    result = await connection.execute(select(table.c.db_name))
    return {str(row["db_name"]) for row in result.mappings()}


def _default_indicator_db_name(raw_value: str, used_db_names: set[str]) -> str:
    if _identifier_byte_length(raw_value) <= POSTGRES_IDENTIFIER_MAX_BYTES and raw_value not in used_db_names:
        return raw_value

    suffix = "_" + hashlib.sha256(raw_value.encode("utf-8")).hexdigest()[:HASH_SUFFIX_LENGTH]
    max_base_bytes = POSTGRES_IDENTIFIER_MAX_BYTES - len(suffix.encode("utf-8"))
    base = _truncate_identifier(raw_value, max_bytes=max_base_bytes).rstrip(" _-")
    candidate = f"{base or 'indicator'}{suffix}"
    counter = 2
    while candidate in used_db_names:
        numbered_suffix = f"{suffix}_{counter}"
        max_base_bytes = POSTGRES_IDENTIFIER_MAX_BYTES - len(numbered_suffix.encode("utf-8"))
        base = _truncate_identifier(raw_value, max_bytes=max_base_bytes).rstrip(" _-")
        candidate = f"{base or 'indicator'}{numbered_suffix}"
        counter += 1
    return candidate


def _truncate_identifier(value: str, *, max_bytes: int) -> str:
    result = ""
    used_bytes = 0
    for char in value:
        char_bytes = len(char.encode("utf-8"))
        if used_bytes + char_bytes > max_bytes:
            break
        result += char
        used_bytes += char_bytes
    return result


def _identifier_byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def _material_keys(raw_materials: Sequence[RawMaterial]) -> list[tuple[str, ...]]:
    keys: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for material in raw_materials:
        for path_length in range(1, len(material.material_path) + 1):
            key = material.material_path[:path_length]
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _material_path(
    material_id: UUID,
    *,
    names_by_id: Mapping[UUID, str],
    parent_ids_by_id: Mapping[UUID, UUID | None],
) -> tuple[str, ...]:
    parent_id = parent_ids_by_id[material_id]
    own_path = (names_by_id[material_id],)
    if parent_id is None:
        return own_path
    return (*_material_path(parent_id, names_by_id=names_by_id, parent_ids_by_id=parent_ids_by_id), *own_path)


async def _replace_data_for_date(
    connection: AsyncConnection,
    table: Table,
    report: ParsedReport,
    *,
    reference_ids: Mapping[str, Mapping[ReferenceKey, UUID]],
) -> int:
    delete_result = await connection.execute(delete(table).where(table.c.loaded_for == report.report_date))
    records = _data_records(report, reference_ids=reference_ids)
    if records:
        await connection.execute(insert(table), records)
    return int(delete_result.rowcount or 0)


def _data_records(report: ParsedReport, *, reference_ids: Mapping[str, Mapping[ReferenceKey, UUID]]) -> list[Record]:
    records: list[Record] = []
    normalized_frame = report.data.astype(object).where(report.data.notna(), None)
    indicator_keys_by_full_name = {indicator.full_name: _indicator_key(indicator) for indicator in report.indicators}
    for row in cast("list[dict[str, object | None]]", normalized_frame.to_dict(orient="records")):
        asset_name = str(row["asset_name"])
        indicator_name = str(row["indicator_name"])
        material_path = cast("tuple[str, ...]", row["material_path"])
        records.append(
            {
                "loaded_for": report.report_date,
                "ordinal_number": row["ordinal_number"],
                "asset_id": reference_ids["assets"][asset_name],
                "material_id": reference_ids["materials"][material_path],
                "indicator_id": reference_ids["indicators"][indicator_keys_by_full_name[indicator_name]],
                "value": row["value"],
                "value_double": _value_as_double(row["value"]),
                "loaded_at": report.loaded_at,
            },
        )
    return records


def _value_as_double(value: object | None) -> float | None:
    result: float | None = None
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        result = float(value)
    elif isinstance(value, str):
        normalized_value = value.strip().replace(" ", "").replace("\u00a0", "")
        if "," in normalized_value and "." not in normalized_value:
            normalized_value = normalized_value.replace(",", ".")
        if normalized_value:
            try:
                result = float(normalized_value)
            except ValueError:
                result = None
    return result


async def _fetch_indicators(connection: AsyncConnection, table: Table) -> tuple[RowMapping, ...]:
    result = await connection.execute(
        select(table.c.id, table.c.db_name, table.c.name).order_by(table.c.ordinal_number, table.c.id),
    )
    return tuple(result.mappings())


async def _comment_pd_view_columns(
    connection: AsyncConnection,
    view_name: str,
    *,
    indicators: Sequence[RowMapping],
) -> None:
    comments_by_column = {
        "loaded_for": "Отчетная дата",
        "ordinal_number": "Порядковый номер строки в выгрузке",
        "asset": "Актив",
        "material": "Сырьевая позиция",
        "parent_material": "Родительская сырьевая позиция",
        "loaded_at": "Дата и время загрузки файла",
        "material_is_gross": "Валовый признак",
        "material_is_commercial": "Товарный признак",
        "material_product_grade": "Марка товара",
    }
    comments_by_column.update({str(indicator["db_name"]): str(indicator["name"]) for indicator in indicators})

    for column_name, comment in comments_by_column.items():
        await connection.execute(
            text(
                f"COMMENT ON COLUMN {view_name}.{quote_identifier(column_name)} IS {quote_sql_literal(comment)}",
            ),
        )


def _pd_view_query(
    tables: PdTables,
    *,
    indicators: Sequence[RowMapping],
    view_reference: str,
    replace: bool,
) -> str:
    select_columns = [
        "d.loaded_for",
        "d.ordinal_number",
        "a.name AS asset",
        "m.name AS material",
        "pm.name AS parent_material",
        "MAX(d.loaded_at) AS loaded_at",
    ]
    for indicator in indicators:
        indicator_id = cast("UUID", indicator["id"])
        db_name = str(indicator["db_name"])
        select_columns.append(
            f"MAX(d.value_double) FILTER (WHERE d.indicator_id = '{indicator_id}') AS {quote_identifier(db_name)}",
        )
    select_columns.extend(
        (
            "m.is_gross AS material_is_gross",
            "m.is_commercial AS material_is_commercial",
            "m.product_grade AS material_product_grade",
        ),
    )

    select_projection = ",\n    ".join(select_columns)
    create_clause = "CREATE OR REPLACE VIEW" if replace else "CREATE VIEW"
    query = f"""
{create_clause} {view_reference} AS
SELECT
    {select_projection}
FROM {qualified_table_name(tables.data)} AS d
JOIN {qualified_table_name(tables.assets)} AS a
    ON a.id = d.asset_id
JOIN {qualified_table_name(tables.materials)} AS m
    ON m.id = d.material_id
LEFT JOIN {qualified_table_name(tables.materials)} AS pm
    ON pm.id = m.parent_id
GROUP BY
    d.loaded_for,
    d.ordinal_number,
    a.id,
    a.name,
    m.id,
    m.name,
    m.is_gross,
    m.is_commercial,
    m.product_grade,
    pm.id,
    pm.name
ORDER BY
    d.loaded_for DESC,
    d.ordinal_number ASC
"""
    return query


async def _create_or_replace_view_with_fallback(
    connection: AsyncConnection,
    *,
    schema_name: str,
    base_view_name: str,
    query_for: Callable[[str], Awaitable[str]],
    fallback_query_for: Callable[[str], Awaitable[str]],
) -> ViewBuildResult:
    try:
        async with connection.begin_nested():
            await connection.execute(text(await query_for(base_view_name)))
        return ViewBuildResult(
            view_name=f"{quote_identifier(schema_name)}.{quote_identifier(base_view_name)}",
            fallback_used=False,
            error_message=None,
        )
    except SQLAlchemyError as error:
        fallback_view_name = await _next_fallback_relation_name(connection, schema_name, base_view_name)
        async with connection.begin_nested():
            await connection.execute(text(await fallback_query_for(fallback_view_name)))
        return ViewBuildResult(
            view_name=f"{quote_identifier(schema_name)}.{quote_identifier(fallback_view_name)}",
            fallback_used=True,
            error_message=str(error),
        )


async def _next_fallback_relation_name(connection: AsyncConnection, schema_name: str, base_view_name: str) -> str:
    candidate = f"{base_view_name}_upd"
    while await _relation_exists(connection, schema_name=schema_name, relation_name=candidate):
        candidate = f"{candidate}_upd"
    return candidate


async def _relation_exists(connection: AsyncConnection, *, schema_name: str, relation_name: str) -> bool:
    return bool(
        await connection.scalar(
            text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM pg_class AS cls
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = cls.relnamespace
                    WHERE namespace.nspname = :schema_name
                      AND cls.relname = :relation_name
                )
                """,
            ),
            {"schema_name": schema_name, "relation_name": relation_name},
        ),
    )


async def _insert_change_log(
    connection: AsyncConnection,
    tables: PdTables,
    changes: Sequence[PdReferenceChange],
    *,
    report: ParsedReport,
) -> None:
    if not changes:
        return

    await connection.execute(
        insert(tables.change_log),
        [
            {
                "id": uuid4(),
                "action": change.action,
                "entity_type": change.entity_type,
                "entity_name": change.entity_name,
                "details": change.details,
                "loaded_for": report.report_date,
                "source_name": report.source_name,
                "created_at": report.loaded_at,
            }
            for change in changes
        ],
    )


async def _grant_select_on_relation(
    connection: AsyncConnection,
    relation_name: str,
    *,
    role_names: Sequence[str],
) -> None:
    for role_name in role_names:
        role_exists = bool(
            await connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
                {"role_name": role_name},
            ),
        )
        if role_exists:
            await connection.execute(text(f"GRANT SELECT ON {relation_name} TO {quote_identifier(role_name)}"))


def _created_changes(entity_type: str, names: Sequence[str]) -> list[PdReferenceChange]:
    return [
        PdReferenceChange(action="created", entity_type=entity_type, entity_name=name, details=None) for name in names
    ]


def pd_relations_to_grant(settings: Settings) -> tuple[str, ...]:
    tables = build_pd_tables(settings)
    return (
        qualified_table_name(tables.assets),
        qualified_table_name(tables.materials),
        qualified_table_name(tables.indicators),
        qualified_table_name(tables.data),
        qualified_table_name(tables.change_log),
        pd_view_reference(settings),
    )
