from pathlib import Path

from excel_parser import convert_excel_value
from sales_op.excel_parser import _bind_source_columns, parse_sales_op_workbook
from sales_op.registry import SOURCE_DEFINITIONS_BY_CODE
from sales_op.specification import SPECIFICATION_FILE_NAME, read_sales_op_specification

PROJECT_DIR = Path(__file__).resolve().parents[1]
MOCK_SALES_OP_DIR = PROJECT_DIR / "mock_data" / "sales_op"


def test_preserved_text_values_are_not_normalized() -> None:
    assert convert_excel_value("", kind="text", cell_reference="A1", preserve_text=True) == ""
    assert convert_excel_value("Null", kind="text", cell_reference="A2", preserve_text=True) == "Null"
    assert convert_excel_value("-", kind="text", cell_reference="A3", preserve_text=True) == "-"


def test_missing_sales_op_column_is_left_unbound() -> None:
    specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)
    mappings = specification.sources["mfk"].mappings

    bound_columns, missing_headers = _bind_source_columns(
        mappings,
        headers=("Производитель", "Продавец"),
        first_column=0,
    )

    assert bound_columns["producer_code"] == 0
    assert bound_columns["seller_code"] == 1
    assert "nomenclative_name" not in bound_columns
    assert "Продукт" in missing_headers


class TestSalesOpSpecification:
    def test_reads_types_and_all_sources(self) -> None:
        specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)

        assert len(specification.columns) == 52
        assert {column.name: column.kind for column in specification.columns}["quantity_t"] == "float"
        assert set(specification.sources) == {
            "mfk",
            "bsk_hshp",
            "bsk_polymers",
            "bsk_mth",
            "bsk_obn",
            "bsk_soda",
        }

    def test_physical_table_contains_only_source_fields(self) -> None:
        specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)
        columns = {column.name for column in specification.sources["bsk_polymers"].table_columns}

        assert "doc_yearmonth" in columns
        assert "source_type" not in columns
        assert "uom_code" not in columns
        assert "uom_k" not in columns
        assert "date_upload" in columns
        assert "date_modified" in columns
        assert "user_name" not in columns

    def test_parses_first_visible_sheet_and_normalizes_values(self) -> None:
        definition = SOURCE_DEFINITIONS_BY_CODE["bsk_polymers"]
        source_path = next(
            path for path in (MOCK_SALES_OP_DIR / "2026-07").glob("*.xlsx") if definition.matches_file_name(path.name)
        )
        specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)

        parsed = parse_sales_op_workbook(
            source_path,
            source_specification=specification.sources[definition.code],
        )

        assert parsed.warning_count > 0
        assert any(value is None for row in parsed.rows for value in row.values())
        assert all("date_upload" in row for row in parsed.rows)
        assert all("date_modified" in row for row in parsed.rows)
        assert all("user_name" not in row for row in parsed.rows)
