"""PD parser tests: the layout rules are the contract, not the SQL around them."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook
from openpyxl.styles import Font

from pd.excel_parser import (
    _find_parent_path,
    _parse_report_date,
    _update_hierarchy_stack,
    clean_asset_name,
    clean_display_name,
    export_reference_csv,
    normalize_cell_value,
    parse_report,
    split_value_for_storage,
    value_to_text,
)

MOCK_PD_WORKBOOK = Path(__file__).resolve().parents[1] / "mock_data" / "pd" / "pd.xlsx"


def _write_pd_workbook(  # noqa: PLR0913
    path: Path,
    *,
    report_date: object = "23.06.26",
    name_1: str = "Показатель 1",
    name_2: str = "Показатель 2",
    name_3: str = "Показатель 3",
    group_3: str = "",
    legacy_column: bool = False,
) -> None:
    """Small first-sheet PD layout: row 6/7 headers, bold assets in E, values in F..H."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Свод"
    sheet["I4"] = report_date
    sheet.merge_cells("F6:G6")
    sheet["F6"] = "Группа"
    sheet["F7"] = name_1
    sheet["G7"] = name_2
    sheet["H6"] = group_3
    sheet["H7"] = name_3

    asset = sheet["E10"]
    asset.value = '"БСК Сода"'
    asset.font = Font(bold=True)
    sheet["F11"] = 10
    sheet["G11"] = "1 234,5"
    sheet["H11"] = True
    sheet["D11"] = 1
    sheet["E11"] = "  Сырьё A"
    sheet["D12"] = 2
    sheet["E12"] = "    Подпозиция"
    sheet["F12"] = 3
    sheet["D13"] = 3
    sheet["E13"] = "  Орехо-Зуево"  # deliberately ignored by the parser
    sheet["F13"] = 99

    second_asset = sheet["E14"]
    second_asset.value = '"АШК" (на 23.06.26)'
    second_asset.font = Font(bold=True)
    sheet["D15"] = 4
    sheet["E15"] = "  Сырьё B"
    sheet["F15"] = "#DIV/0!"
    sheet["G15"] = date(2026, 6, 22)
    sheet["H15"] = None

    if legacy_column:
        sheet["AK6"] = "Старый показатель"
        sheet["AK7"] = "Не должен попасть в список"
        sheet["AK11"] = 777

    other = workbook.create_sheet("Другой лист")
    other["E10"] = "Игнорируемый актив"
    workbook.save(path)


class TestParseReport:
    def test_parses_checked_in_pd_workbook_with_exact_counts(self) -> None:
        report = parse_report(MOCK_PD_WORKBOOK, loaded_at=datetime(2026, 6, 24, tzinfo=UTC))

        assert report.source_name == "pd.xlsx"
        assert report.report_date == date(2026, 6, 23)
        assert report.loaded_at == datetime(2026, 6, 24, tzinfo=UTC)
        assert len(report.assets) == 14
        assert len(report.raw_materials) == 91
        assert len(report.indicators) == 30
        assert len(report.data) == 2730
        assert [indicator.xls_col for indicator in report.indicators][:2] == ["F", "G"]
        assert [indicator.xls_col for indicator in report.indicators][-1] == "AI"
        assert set(report.data.columns) == {
            "asset_name",
            "raw_material_name",
            "material_path",
            "ordinal_number",
            "indicator_name",
            "value",
            "loaded_at",
        }

    def test_uses_only_the_first_sheet_and_the_approved_indicator_range(self, tmp_path: Path) -> None:
        path = tmp_path / "small.xlsx"
        _write_pd_workbook(path, legacy_column=True)

        report = parse_report(path, loaded_at=datetime(2026, 6, 24, tzinfo=UTC))

        assert report.report_date == date(2026, 6, 23)
        assert [asset.name for asset in report.assets] == ["БСК Сода", "АШК"]
        assert [indicator.full_name for indicator in report.indicators] == [
            "Группа | Показатель 1",
            "Группа | Показатель 2",
            "Показатель 3",
        ]
        assert not any("Старый показатель" in indicator.full_name for indicator in report.indicators)
        assert not any("Игнорируемый" in asset.name for asset in report.assets)
        assert len(report.data) == 3 * 3  # three kept materials times three indicators
        assert report.data.loc[0, "value"] == "10"
        assert report.data.loc[1, "value"] == "1 234,5"
        assert report.data.loc[2, "value"] == "TRUE"
        assert pd.isna(report.data.loc[6, "value"])  # #DIV/0! in the second asset row

    def test_hierarchy_parent_and_ignored_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "hierarchy.xlsx"
        _write_pd_workbook(path)

        report = parse_report(path, loaded_at=datetime(2026, 6, 24, tzinfo=UTC))

        assert [material.name for material in report.raw_materials] == ["Сырьё A", "Подпозиция", "Сырьё B"]
        assert [material.ordinal_number for material in report.raw_materials] == [1, 2, 3]
        assert report.raw_materials[0].parent_name is None
        assert report.raw_materials[0].material_path == ("Сырьё A",)
        assert report.raw_materials[1].parent_name == "Сырьё A"
        assert report.raw_materials[1].material_path == ("Сырьё A", "Подпозиция")
        assert report.raw_materials[2].parent_name is None  # a new asset resets the hierarchy
        assert [material.xls_row for material in report.raw_materials] == [11, 12, 15]

    def test_duplicate_normalized_indicator_names_are_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "duplicate.xlsx"
        _write_pd_workbook(path, name_3="Показатель 1", group_3="Группа")

        with pytest.raises(ValueError, match="неуникальные имена колонок"):
            parse_report(path, loaded_at=datetime(2026, 6, 24, tzinfo=UTC))

    def test_unnamed_indicator_column_with_data_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "unnamed.xlsx"
        _write_pd_workbook(path, name_3="")

        with pytest.raises(ValueError, match="Не удалось определить имя показателя"):
            parse_report(path, loaded_at=datetime(2026, 6, 24, tzinfo=UTC))

    def test_missing_file_is_reported_before_opening_workbooks(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Файл не найден"):
            parse_report(tmp_path / "absent.xlsx", loaded_at=datetime(2026, 6, 24, tzinfo=UTC))

    def test_export_reference_csv_writes_all_three_frames(self, tmp_path: Path) -> None:
        small = tmp_path / "small.xlsx"
        _write_pd_workbook(small)
        report = parse_report(small, loaded_at=datetime(2026, 6, 24, tzinfo=UTC))

        output = tmp_path / "export"
        export_reference_csv(report, output)

        assert {path.name for path in output.iterdir()} == {
            "assets.csv",
            "raw_materials.csv",
            "indicators.csv",
        }
        assert "name,xls_row,loaded_at" in (output / "assets.csv").read_text(encoding="utf-8-sig")


class TestReportDate:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (datetime(2026, 6, 23, 15, 30, tzinfo=UTC), date(2026, 6, 23)),
            (date(2026, 6, 23), date(2026, 6, 23)),
            ("2026-06-23", date(2026, 6, 23)),
            ("23.06.26", date(2026, 6, 23)),
            ("23.06.1968", date(1968, 6, 23)),
            ("23.06.68", date(2068, 6, 23)),
            ("23.06.69", date(1969, 6, 23)),
        ],
    )
    def test_accepts_excel_text_and_date_objects(self, value: object, expected: date) -> None:
        assert _parse_report_date(value) == expected

    @pytest.mark.parametrize("value", [None, "not a date", "31.02.2026", "23..26", "2026-6-23", 45_000])
    def test_rejects_unreadable_values(self, value: object) -> None:
        with pytest.raises(ValueError, match="Не удалось прочитать дату отчета"):
            _parse_report_date(value)


class TestNormalization:
    @pytest.mark.parametrize("value", [None, float("nan"), "", "-", "#DIV/0!", "#MYERR!"])
    def test_empty_and_error_cells_become_none(self, value: object) -> None:
        assert normalize_cell_value(value) is None

    def test_collapses_spaces_and_non_breaking_spaces(self) -> None:
        assert normalize_cell_value("  БСК\u00a0  Сода  ") == "БСК Сода"
        assert normalize_cell_value("1   234,5") == "1 234,5"

    @pytest.mark.parametrize(
        "value",
        [True, 0, 1.5, Decimal("2.5"), date(2026, 6, 23), datetime(2026, 6, 23, tzinfo=UTC)],
    )
    def test_keeps_typed_values(self, value: object) -> None:
        assert normalize_cell_value(value) == value
        assert type(normalize_cell_value(value)) is type(value)

    def test_falls_back_to_string_for_unknown_objects(self) -> None:
        class _Labelled:
            def __str__(self) -> str:
                return "  some   value  "

        assert normalize_cell_value(_Labelled()) == "some value"


class TestStorageValues:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, (None, None)),
            (True, (None, "TRUE")),
            (False, (None, "FALSE")),
            (7, (Decimal(7), None)),
            (Decimal("1.20"), (Decimal("1.20"), None)),
            (1.5, (Decimal("1.5"), None)),
            (float("nan"), (None, None)),
            (float("inf"), (None, None)),
            (datetime(2026, 6, 23, 15, 30, tzinfo=UTC), (None, "2026-06-23")),
            (date(2026, 6, 23), (None, "2026-06-23")),
            ("1 234,5", (Decimal("1234.5"), None)),
            ("abc", (None, "abc")),
        ],
    )
    def test_splits_into_double_and_text_parts(self, value: object, expected: tuple[object, object]) -> None:
        assert split_value_for_storage(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            (Decimal("7.5"), "7.5"),
            ("abc", "abc"),
            (True, "TRUE"),
            (date(2026, 6, 23), "2026-06-23"),
        ],
    )
    def test_text_projection(self, value: object, expected: str | None) -> None:
        assert value_to_text(value) == expected


class TestNamesAndHierarchy:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("  Plain   name ", "Plain name"),
            ('  "Quoted name"  ', "Quoted name"),
            ("«Quoted name»", "Quoted name"),
            (None, ""),
        ],
    )
    def test_display_name(self, value: object, expected: str) -> None:
        assert clean_display_name(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ('"БСК Сода"', "БСК Сода"),
            ('"БСК Сода" (на 23.06.26)', "БСК Сода"),
            ('"БСК Сода" (НА 23.06.26)', "БСК Сода"),
            ("БСК Сода (на 23.06.26)", "БСК Сода (на 23.06.26)"),
        ],
    )
    def test_asset_name_drops_the_report_date_suffix(self, value: str, expected: str) -> None:
        assert clean_asset_name(value) == expected

    def test_hierarchy_stack_tracks_the_nearest_parent_and_replaces_deeper_levels(self) -> None:
        stack: dict[int, tuple[str, ...]] = {}

        assert _find_parent_path(stack, 0) == ()
        _update_hierarchy_stack(stack, 0, ("Группа",))
        _update_hierarchy_stack(stack, 2, ("Группа", "Позиция"))

        assert _find_parent_path(stack, 1) == ("Группа",)
        assert _find_parent_path(stack, 3) == ("Группа", "Позиция")

        _update_hierarchy_stack(stack, 2, ("Группа", "Другая позиция"))
        assert _find_parent_path(stack, 3) == ("Группа", "Другая позиция")
        assert _find_parent_path(stack, 2) == ("Группа",)


def test_frames_expose_nullable_integer_columns(tmp_path: Path) -> None:
    path = tmp_path / "small.xlsx"
    _write_pd_workbook(path)
    report = parse_report(path, loaded_at=datetime(2026, 6, 24, tzinfo=UTC))

    assert str(report.assets_frame["xls_row"].dtype) == "Int64"
    assert str(report.raw_materials_frame["ordinal_number"].dtype) == "Int64"
    assert str(report.indicators_frame["xls_col_index"].dtype) == "Int64"
    assert len(report.indicators_frame) == 3
