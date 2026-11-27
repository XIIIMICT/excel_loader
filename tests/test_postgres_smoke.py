"""
End-to-end PostgreSQL smoke test: real service, real runners, disposable schemas.

EL has no migration tool; the loader itself applies its DDL on the first run
(``prepare_source_history``, PD ``_ensure_pd_storage`` and sales_op's additive
``ensure_additive_table_structure``). The test resets both schemas first, then
checks that every expected relation exists after the first run.

No production data is used: PD reads the checked-in ``mock_data/pd/pd.xlsx`` and
sales_op reads tiny workbooks generated in a temporary folder from the checked-in
specification. The second run exercises monthly partition replacement, a
neighbouring month and a legacy extra column added between the runs.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.engine import make_url

from config import MODULE_CODES, Settings
from db import create_postgres_engine
from main import _build_parser, _run_app
from sales_op.runner import parse_sales_op_options
from sales_op.specification import SPECIFICATION_FILE_NAME
from schema_definition import quote_identifier

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL is not set; PostgreSQL smoke test skipped", allow_module_level=True)

PROJECT_DIR = Path(__file__).resolve().parents[1]
PD_FIXTURE = PROJECT_DIR / "mock_data" / "pd" / "pd.xlsx"
SALES_OP_SPECIFICATION = PROJECT_DIR / "mock_data" / "sales_op" / SPECIFICATION_FILE_NAME
PLAN_NAME = "МФК_Поклиентный план_Рейтинг_июль.xlsx"
MONTHS = (date(2026, 6, 1), date(2026, 7, 1))
EXPECTED_PD_ROWS = 2730
EXPECTED_ASSETS = 14
EXPECTED_MATERIALS = 87  # 91 parsed rows, four share a (name, parent) reference
EXPECTED_INDICATORS = 30


def _write_plan(path: Path, month: date) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "План продаж :"
    sheet["B1"] = month.isoformat()
    headers = (
        "Производитель",
        "Продавец",
        "Номенклатурная группа",
        "Наименование контрагента",
        "Объем продаж, тонн",
        "Комментарии",
    )
    for column, header in enumerate(headers, start=1):
        sheet.cell(row=2, column=column, value=header)
    sheet.append(("P1", "S1", "G1", "Client A", 10, None))
    sheet.append((None,) * len(headers))  # the parser stops here
    sheet.append(("P2", "S2", "G2", "Client B", 30, None))  # must not be read
    workbook.save(path)


@pytest.fixture(scope="module")
def smoke_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("pg-smoke")
    pd_dir = root / "pd"
    sales_dir = root / "sales_op"
    pd_dir.mkdir()
    sales_dir.mkdir()
    shutil.copy(PD_FIXTURE, pd_dir / PD_FIXTURE.name)
    shutil.copy(SALES_OP_SPECIFICATION, sales_dir / SPECIFICATION_FILE_NAME)
    for month in MONTHS:
        month_dir = sales_dir / f"{month.year:04d}-{month.month:02d}"
        month_dir.mkdir()
        _write_plan(month_dir / PLAN_NAME, month)
    return root


@pytest.fixture(scope="module")
def smoke_settings(smoke_root: Path) -> Settings:
    url = make_url(TEST_DATABASE_URL or "")
    schema = os.environ.get("TEST_SCHEMA", "el_smoke")
    module_data_dirs = {"pd": smoke_root / "pd", "sales_op": smoke_root / "sales_op"}
    return Settings.model_validate(
        {
            "common": {
                "timezone": "UTC",
                "log_dir": str(smoke_root / "logs"),
                "unloaded_data_dir": str(smoke_root / "unloaded_data"),
                "postgres": {
                    "host": url.host or "localhost",
                    "port": url.port or 5432,
                    "user": url.username or "postgres",
                    "password": url.password or "postgres",
                    "db": url.database or "dwh",
                },
            },
            **{
                code: {
                    "schema": schema,
                    "sources": {"data_dir": str(module_data_dirs.get(code, smoke_root / "mock_data"))},
                }
                for code in MODULE_CODES
            },
        },
    )


def _qualified(schema: str, name: str) -> str:
    return f"{quote_identifier(schema)}.{quote_identifier(name)}"


async def _reset_schemas(settings: Settings, module_schema: str, *, drop_only: bool) -> None:
    # Create and dispose the engine inside this coroutine: an asyncio.Runner
    # cannot reuse pooled connections from a previous event loop.
    engine = create_postgres_engine(settings)
    try:
        async with engine.begin() as connection:
            for name in ("el", module_schema):
                identifier = quote_identifier(name)
                await connection.execute(text(f"DROP SCHEMA IF EXISTS {identifier} CASCADE"))
                if not drop_only:
                    await connection.execute(text(f"CREATE SCHEMA {identifier}"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def _disposable_database(smoke_settings: Settings) -> Iterator[None]:
    schema_name = smoke_settings.modules["pd"].schema_name
    asyncio.run(_reset_schemas(smoke_settings, schema_name, drop_only=False))
    try:
        yield
    finally:
        asyncio.run(_reset_schemas(smoke_settings, schema_name, drop_only=True))


async def _rows(
    engine: AsyncEngine,
    statement: str,
    parameters: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        result = await connection.execute(text(statement), parameters or {})
        return [dict(row) for row in result.mappings()]


async def _count(engine: AsyncEngine, table_reference: str) -> int:
    rows = await _rows(engine, f"SELECT COUNT(*) AS total FROM {table_reference}")  # noqa: S608
    return int(cast("int", rows[0]["total"]))


async def _relation_exists(engine: AsyncEngine, schema_name: str, relation_name: str) -> bool:
    rows = await _rows(
        engine,
        """
        SELECT EXISTS(
            SELECT 1 FROM pg_class AS cls
            JOIN pg_namespace AS namespace ON namespace.oid = cls.relnamespace
            WHERE namespace.nspname = :schema_name AND cls.relname = :relation_name
        ) AS exists_flag
        """,
        {"schema_name": schema_name, "relation_name": relation_name},
    )
    return bool(rows[0]["exists_flag"])


async def _run_loader(settings: Settings, *, months: str | None = None) -> int:
    argv = ["--module", "pd", "--module", "sales_op", "--source", "local"]
    if months is not None:
        argv.extend(("--months", months, "--force"))
    args = _build_parser().parse_args(argv)
    options = parse_sales_op_options(force=months is not None, months=months)
    return await _run_app(settings, args=args, sales_op_options=options)


async def test_postgres_smoke_end_to_end(smoke_settings: Settings) -> None:
    schema = smoke_settings.modules["pd"].schema_name
    engine = create_postgres_engine(smoke_settings)
    try:
        # -- First real loader run: applies DDL and loads both modules.
        first_exit_code = await _run_loader(smoke_settings, months="2026-06,2026-07")

        assert first_exit_code == 1  # a fresh schema creates PD reference rows -> has_format_changes
        expected_relations = (
            "ref_assets",
            "ref_materials",
            "ref_indicators",
            "pd_data",
            "pd_reference_change_log",
            "pd_view",
            "sales_op_mfk",
            "sales_op_raw_view",
        )
        for relation in expected_relations:
            assert await _relation_exists(engine, schema, relation), relation

        assert await _count(engine, _qualified(schema, "pd_data")) == EXPECTED_PD_ROWS
        assert await _count(engine, _qualified(schema, "ref_assets")) == EXPECTED_ASSETS
        assert await _count(engine, _qualified(schema, "ref_materials")) == EXPECTED_MATERIALS
        assert await _count(engine, _qualified(schema, "ref_indicators")) == EXPECTED_INDICATORS
        assert await _count(engine, _qualified(schema, "pd_reference_change_log")) == (
            EXPECTED_ASSETS + EXPECTED_MATERIALS + EXPECTED_INDICATORS
        )
        assert await _count(engine, _qualified(schema, "sales_op_mfk")) == len(MONTHS)
        assert await _count(engine, _qualified(schema, "sales_op_raw_view")) == len(MONTHS)
        assert await _count(engine, "el.excel_loader_sources_history") == 3

        first_rows = await _rows(
            engine,
            f"SELECT doc_yearmonth, client_name, quantity_t FROM {_qualified(schema, 'sales_op_mfk')} "  # noqa: S608
            "ORDER BY doc_yearmonth",
        )
        assert [row["doc_yearmonth"] for row in first_rows] == list(MONTHS)
        assert [(row["client_name"], row["quantity_t"]) for row in first_rows] == [("Client A", 10.0)] * len(MONTHS)

        # -- Simulate a legacy column from an older deployment, then reload only July.
        table = _qualified(schema, "sales_op_mfk")
        async with engine.begin() as connection:
            await connection.execute(text(f"ALTER TABLE {table} ADD COLUMN legacy_note TEXT"))
            await connection.execute(
                text(f"UPDATE {table} SET legacy_note = 'kept' WHERE doc_yearmonth = DATE '2026-06-01'"),  # noqa: S608
            )
        selection = (
            f"SELECT doc_yearmonth, client_name, quantity_t, legacy_note FROM {table} "  # noqa: S608
            "ORDER BY doc_yearmonth"
        )
        before_second_run = await _rows(engine, selection)

        second_exit_code = await _run_loader(smoke_settings, months="2026-07")

        assert second_exit_code == 0  # no new reference rows, no errors on the repeat run
        assert await _rows(engine, selection) == before_second_run
        assert await _count(engine, _qualified(schema, "pd_data")) == EXPECTED_PD_ROWS
        assert await _count(engine, _qualified(schema, "pd_reference_change_log")) == (
            EXPECTED_ASSETS + EXPECTED_MATERIALS + EXPECTED_INDICATORS
        )
        legacy_columns = await _rows(
            engine,
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = :schema_name AND table_name = 'sales_op_mfk'
            """,
            {"schema_name": schema},
        )
        assert "legacy_note" in [row["column_name"] for row in legacy_columns]
    finally:
        await engine.dispose()
