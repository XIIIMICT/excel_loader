import asyncio
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

SRC_DIR = Path(__file__).resolve().parents[1]

while str(SRC_DIR) in sys.path:
    sys.path.remove(str(SRC_DIR))
sys.path.insert(0, str(SRC_DIR))


# ruff: disable[E402]
from sqlalchemy import delete, text
from sqlalchemy.schema import CreateSchema

from app_logging import configure_logging, get_logger
from config import Settings, load_settings
from db import postgres_session
from pd.db import load_report_to_db
from pd.excel_parser import parse_report
from pd.schema import PdTables, build_pd_tables
from postgres import grant_select_on_table_to_existing_roles, grant_usage_on_schema_to_existing_roles
from schema_definition import quote_identifier

# ruff: enable[E402]

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

    from pd.models import Indicator, ParsedReport

LOGGER = get_logger("seed")
SEED_PD_SOURCE_PATH = Path("pd") / "pd.xlsx"

SEED_INDICATOR_DB_NAMES_BY_XLS_COL = {
    "F": "production_daily_t",
    "G": "bp_2026_month_t",
    "H": "bp_to_date_calc_t",
    "I": "operational_plan_month_t",
    "J": "production_mtd_plan_t",
    "K": "production_mtd_actual_t",
    "L": "production_mtd_deviation_t",
    "M": "production_mtd_rhythm_pct",
    "N": "shipment_mtd_total_bp_2026_plan_t",
    "O": "shipment_mtd_total_operational_plan_t",
    "P": "shipment_mtd_total_actual_t",
    "Q": "shipment_mtd_total_rhythm_pct",
    "R": "total_supply_production_and_receipts_t",
    "S": "consumption_mtd_tech_process_t",
    "T": "consumption_mtd_other_needs_t",
    "U": "consumption_norm_output_t",
    "V": "consumption_norm_production_mtd_t",
    "W": "consumption_norm_all_outputs_t",
    "X": "daily_consumption_norm_deviation_t",
    "Y": "stock_norm_fact_t",
    "Z": "stock_norm_pct",
    "AA": "demand_ytd_bp_to_date_t",
    "AB": "demand_ytd_actual_to_date_t",
    "AC": "demand_ytd_deviation_t",
    "AD": "demand_ytd_deviation_pct",
    "AE": "annual_consumption_2025_t",
    "AF": "annual_consumption_bp_2026_t",
    "AG": "annual_consumption_forecast_t",
    "AH": "annual_consumption_fact_t",
    "AI": "annual_consumption_fact_pct",
}


def main() -> int:
    try:
        settings = load_settings()
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 2

    started_at = datetime.now(tz=settings.app_timezone)
    configure_logging(settings, started_at)
    return asyncio.run(_run(settings))


async def _run(settings: Settings) -> int:
    try:
        await seed_pd(settings)
    except Exception:
        LOGGER.exception("Seed PD завершился с ошибкой.")
        return 1
    return 0


async def seed_pd(settings: Settings) -> None:
    source_path = settings.mock_data_dir / SEED_PD_SOURCE_PATH
    loaded_at = datetime.now(tz=settings.app_timezone)
    report = _with_seed_indicator_db_names(parse_report(source_path, loaded_at=loaded_at))
    tables = build_pd_tables(settings)

    async with postgres_session(settings) as engine:
        async with engine.begin() as connection:
            if tables.metadata.schema is not None:
                await connection.execute(CreateSchema(tables.metadata.schema, if_not_exists=True))
            await _drop_pd_views(settings, connection)
            await connection.run_sync(tables.metadata.drop_all)
            await connection.run_sync(tables.metadata.create_all)
        await load_report_to_db(settings, engine, report)
        async with engine.begin() as connection:
            await connection.execute(delete(tables.data))

        await _grant_select(settings, engine, tables)

    LOGGER.info(
        "Seed PD завершен. Источник=%s, assets=%s, materials=%s, indicators=%s, data очищена.",
        source_path,
        len(report.assets),
        len(report.raw_materials),
        len(report.indicators),
    )


async def _grant_select(settings: Settings, engine: AsyncEngine, tables: PdTables) -> None:
    if not settings.pd_grant_select_users:
        return

    granted_roles, skipped_roles = await grant_usage_on_schema_to_existing_roles(
        engine,
        schema_name=settings.pd_schema,
        role_names=settings.pd_grant_select_users,
    )
    if granted_roles:
        LOGGER.info("Выданы права USAGE на схему %s пользователям: %s.", settings.pd_schema, ", ".join(granted_roles))
    if skipped_roles:
        LOGGER.info(
            "Пользователи не найдены и пропущены при выдаче USAGE на схему %s: %s.",
            settings.pd_schema,
            ", ".join(skipped_roles),
        )

    for table in (tables.assets, tables.materials, tables.indicators, tables.data, tables.change_log):
        granted_roles, skipped_roles = await grant_select_on_table_to_existing_roles(
            engine,
            table=table,
            role_names=settings.pd_grant_select_users,
        )
        table_reference = f"{table.schema}.{table.name}"
        if granted_roles:
            LOGGER.info(
                "Для таблицы %s выданы права SELECT пользователям: %s.",
                table_reference,
                ", ".join(granted_roles),
            )
        if skipped_roles:
            LOGGER.info(
                "Пользователи не найдены и пропущены при выдаче SELECT для таблицы %s: %s.",
                table_reference,
                ", ".join(skipped_roles),
            )


async def _drop_pd_views(settings: Settings, connection: AsyncConnection) -> None:
    view_prefix = settings.pd_table_name("pd_view")
    result = await connection.execute(
        text(
            """
            SELECT cls.relname
            FROM pg_class AS cls
            JOIN pg_namespace AS namespace
              ON namespace.oid = cls.relnamespace
            WHERE namespace.nspname = :schema_name
              AND cls.relkind IN ('v', 'm')
              AND cls.relname LIKE :view_pattern
            ORDER BY cls.relname DESC
            """,
        ),
        {"schema_name": settings.pd_schema, "view_pattern": f"{view_prefix}%"},
    )
    for view_name in [str(row["relname"]) for row in result.mappings()]:
        qualified_view_name = f"{quote_identifier(settings.pd_schema)}.{quote_identifier(view_name)}"
        await connection.execute(
            text(f"DROP VIEW IF EXISTS {qualified_view_name} CASCADE"),
        )


def _with_seed_indicator_db_names(report: ParsedReport) -> ParsedReport:
    return replace(
        report,
        indicators=tuple(_with_seed_indicator_db_name(indicator) for indicator in report.indicators),
    )


def _with_seed_indicator_db_name(indicator: Indicator) -> Indicator:
    db_name = SEED_INDICATOR_DB_NAMES_BY_XLS_COL.get(indicator.xls_col or "")
    if db_name is None:
        return indicator
    return replace(indicator, db_name=db_name)


if __name__ == "__main__":
    raise SystemExit(main())
