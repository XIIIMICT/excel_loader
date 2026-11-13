import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from openpyxl import Workbook

from dictionary_models import WorkbookMetadata
from excel_parser import convert_excel_value
from sales_op.excel_parser import _bind_source_columns, parse_sales_op_workbook
from sales_op.models import SelectedSalesOpFile
from sales_op.runner import _mark_processed_files
from sales_op.specification import SPECIFICATION_FILE_NAME, _read_sources, read_sales_op_specification
from source_resolver import _is_processed_sales_op_file, _resolve_folder_sources_by_pattern

if TYPE_CHECKING:
    import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
MOCK_SALES_OP_DIR = PROJECT_DIR / "mock_data" / "sales_op"


def test_preserved_text_values_are_not_normalized() -> None:
    assert convert_excel_value("", kind="text", cell_reference="A1", preserve_text=True) == ""
    assert convert_excel_value("Null", kind="text", cell_reference="A2", preserve_text=True) == "Null"
    assert convert_excel_value("-", kind="text", cell_reference="A3", preserve_text=True) == "-"


def test_missing_optional_sales_op_column_is_left_unbound() -> None:
    specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)
    mappings = specification.sources["mfk"].mappings

    bound_columns, missing_headers = _bind_source_columns(
        mappings,
        headers=("Производитель", "Продавец", "Номенклатурная группа"),
        first_column=0,
    )

    assert bound_columns["producer_code"] == 0
    assert bound_columns["seller_code"] == 1
    # An optional column may be absent: it is left unbound and becomes NULL.
    assert "nomenclative_name" not in bound_columns
    assert missing_headers == ()


def test_missing_not_nullable_sales_op_column_is_reported() -> None:
    specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)
    mappings = specification.sources["mfk"].mappings

    _bound_columns, missing_headers = _bind_source_columns(
        mappings,
        headers=("Продавец",),
        first_column=0,
    )

    # producer_code and nomenclative_group_name are "Not Nullable" -> required.
    assert set(missing_headers) == {"Производитель", "Номенклатурная группа"}


class TestSalesOpSpecification:
    def test_reads_types_and_all_sources(self) -> None:
        specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)

        assert len(specification.columns) == 54
        assert {column.name: column.kind for column in specification.columns}["quantity_t"] == "float"
        assert set(specification.sources) == {
            "mfk",
            "bsk_hshp",
            "bsk_polymers",
            "bsk_mth",
            "bsk_obn",
            "bsk_soda",
        }
        assert {column.name for column in specification.columns if not column.nullable} == {
            "doc_yearmonth",
            "producer_code",
            "nomenclative_group_name",
        }

    def test_file_patterns_come_from_the_specification(self) -> None:
        specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)

        assert specification.sources["mfk"].file_pattern.endswith(r"\.xlsx$")
        assert specification.sources["mfk"].matches_file_name("МФК_Поклиентный план_июль.xlsx")
        assert not specification.sources["mfk"].matches_file_name("БСК_сода_план.xlsx")

    def test_source_without_file_mask_is_skipped(self) -> None:
        rows: list[list[object]] = [[None] * 11 for _ in range(9)]
        rows[0][7] = "Источник с маской"
        rows[8][7] = "el.sales_op_bsk_soda"
        rows[7][7] = r"^(?=.*сода).*\.xlsx$"
        rows[0][9] = "Источник без маски"
        rows[8][9] = "el.sales_op_bsk_mth"
        rows[7][9] = None

        sources = _read_sources(rows)

        assert [code for code, _index, _pattern in sources] == ["bsk_soda"]

    def test_physical_table_contains_only_source_fields(self) -> None:
        specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)
        columns = {column.name for column in specification.sources["bsk_polymers"].table_columns}

        assert "doc_yearmonth" in columns
        assert "source_type" not in columns
        assert "uom_code" not in columns
        assert "uom_k" not in columns
        assert "date_upload" not in columns
        assert "date_modified" not in columns
        assert "user_name" not in columns

    def test_parses_first_visible_sheet_and_normalizes_values(self) -> None:
        specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)
        definition = specification.sources["bsk_polymers"]
        source_path = next(
            path for path in (MOCK_SALES_OP_DIR / "2026-07").glob("*.xlsx") if definition.matches_file_name(path.name)
        )

        parsed = parse_sales_op_workbook(
            source_path,
            source_specification=specification.sources[definition.code],
        )

        assert parsed.warning_count > 0
        assert any(value is None for row in parsed.rows for value in row.values())
        assert all("date_upload" not in row for row in parsed.rows)
        assert all("date_modified" not in row for row in parsed.rows)
        assert all("user_name" not in row for row in parsed.rows)


def _synthetic_plan(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "План продаж :"
    sheet["B1"] = "2026-07-01"
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
    sheet.append(("P1", "S1", "G1", None, 20, None))  # empty client -> skipped
    sheet.append((None,) * len(headers))  # fully blank -> stop
    sheet.append(("P2", "S2", "G2", "Client B", 30, None))  # must not be read
    workbook.save(path)


def test_skips_rows_without_client_name_and_stops_at_blank_row(tmp_path: Path) -> None:
    specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)
    source_path = tmp_path / "plan.xlsx"
    _synthetic_plan(source_path)

    parsed = parse_sales_op_workbook(
        source_path,
        source_specification=specification.sources["bsk_polymers"],
    )

    assert len(parsed.rows) == 1
    assert parsed.rows[0]["client_name"] == "Client A"
    assert parsed.rows[0]["producer_code"] == "P1"


def test_resolve_folder_sources_skips_underscore_prefixed_files(tmp_path: Path) -> None:
    (tmp_path / "plan.xlsx").write_bytes(b"")
    (tmp_path / "_processed_plan.xlsx").write_bytes(b"")

    sources = _resolve_folder_sources_by_pattern(
        data_dir=tmp_path,
        file_pattern=r".*\.xlsx$",
        allow_empty=True,
        skip_processed_sales_op_files=True,
    )

    assert [source.path.name for source in sources] == ["plan.xlsx"]
    assert _is_processed_sales_op_file("_processed_plan.xlsx")

    # Other modules must not inherit the sales_op-specific marker filter.
    unfiltered = _resolve_folder_sources_by_pattern(
        data_dir=tmp_path,
        file_pattern=r".*\.xlsx$",
        allow_empty=True,
    )
    assert [source.path.name for source in unfiltered] == ["_processed_plan.xlsx", "plan.xlsx"]


def _selected_file(path: Path, source_code: str = "mfk") -> SelectedSalesOpFile:
    moment = datetime(2026, 7, 25, tzinfo=UTC)
    return SelectedSalesOpFile(
        source_code=source_code,
        path=path,
        display_name=str(path),
        original_name=path.name,
        plan_date=date(2026, 7, 1),
        modified_at=moment,
        metadata=WorkbookMetadata("tester", moment, moment),
        history_reference=str(path),
    )


def test_mark_processed_files_renames_only_loaded_sources(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    loaded = tmp_path / "loaded.xlsx"
    other = tmp_path / "other.xlsx"
    loaded.write_bytes(b"")
    other.write_bytes(b"")

    with caplog.at_level(logging.INFO, logger="excel_loader"):
        warnings = _mark_processed_files(
            (_selected_file(loaded), _selected_file(other, source_code="bsk_soda")),
            loaded_source_codes={"mfk"},
        )

    assert warnings == 0
    assert not loaded.exists()
    assert (tmp_path / "_loaded.xlsx").exists()
    assert other.exists(), "a source whose table was not loaded must be retried next run"


def test_mark_processed_files_warns_when_rename_fails(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    loaded = tmp_path / "loaded.xlsx"
    loaded.write_bytes(b"")
    target = tmp_path / "_loaded.xlsx"
    target.mkdir()  # rename onto an existing directory fails

    with caplog.at_level(logging.WARNING, logger="excel_loader"):
        warnings = _mark_processed_files(
            (_selected_file(loaded),),
            loaded_source_codes={"mfk"},
        )

    assert warnings == 1
    assert loaded.exists()
    assert any("не удалось переименовать" in record.getMessage() for record in caplog.records)
