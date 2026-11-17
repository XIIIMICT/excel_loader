import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from openpyxl import Workbook

from excel_parser import convert_excel_value
from sales_op.db import _next_month_start, _source_select, build_sales_op_table
from sales_op.excel_parser import _bind_source_columns, parse_sales_op_workbook
from sales_op.models import SalesOpError, SalesOpRunOptions
from sales_op.runner import (
    _collect_month_files,
    _parse_selected_files,
    _select_month_folders,
    _select_source_files,
    parse_sales_op_options,
)
from sales_op.specification import SPECIFICATION_FILE_NAME, _read_sources, read_sales_op_specification
from source_resolver import SourceFile, _resolve_folder_sources_by_pattern

if TYPE_CHECKING:
    from config import Settings

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


def _synthetic_plan(path: Path, plan_date: str = "2026-07-01", *, junk_date_cell: str | None = None) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "План продаж :"
    sheet["B1"] = plan_date
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
    if junk_date_cell is not None:
        # A date-formatted serial outside Excel's range, the way real workbooks
        # carry stray cells far to the right of the plan block.
        sheet[junk_date_cell] = 99999999
        sheet[junk_date_cell].number_format = "dd.mm.yyyy"
    workbook.save(path)


def _plan_without_headers(path: Path, plan_date: str) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "План продаж :"
    sheet["B1"] = plan_date
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


def test_far_right_date_serial_is_not_logged_as_a_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)
    source_path = tmp_path / "plan.xlsx"
    _synthetic_plan(source_path, junk_date_cell="BE3")

    with caplog.at_level(logging.DEBUG, logger="excel_loader"):
        parsed = parse_sales_op_workbook(
            source_path,
            source_specification=specification.sources["bsk_polymers"],
        )

    assert len(parsed.rows) == 1
    date_warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "датой вне диапазона" in record.getMessage()
    ]
    assert date_warnings == []
    debug_messages = [record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG]
    assert any("вне читаемых столбцов: 1" in message for message in debug_messages), debug_messages


def test_date_serial_in_a_bound_column_is_logged_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)
    source_path = tmp_path / "plan.xlsx"
    _synthetic_plan(source_path, junk_date_cell="F3")  # «Комментарии», a bound column

    with caplog.at_level(logging.WARNING, logger="excel_loader"):
        parsed = parse_sales_op_workbook(
            source_path,
            source_specification=specification.sources["bsk_polymers"],
        )

    assert len(parsed.rows) == 1
    assert parsed.warning_count == 1
    date_warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "датой вне диапазона" in record.getMessage()
    ]
    assert len(date_warnings) == 1, date_warnings
    assert "F3=99999999" in date_warnings[0]


def test_resolve_folder_sources_finds_underscore_prefixed_files(tmp_path: Path) -> None:
    (tmp_path / "plan.xlsx").write_bytes(b"")
    (tmp_path / "_processed_plan.xlsx").write_bytes(b"")

    sources = _resolve_folder_sources_by_pattern(
        data_dir=tmp_path,
        file_pattern=r".*\.xlsx$",
        allow_empty=True,
    )

    # The underscore is a sales_op marker, not a general source filter.
    assert [source.path.name for source in sources] == ["_processed_plan.xlsx", "plan.xlsx"]


class TestSalesOpOptions:
    def test_default_options_select_the_current_month(self) -> None:
        assert parse_sales_op_options(force=False, months=None) == SalesOpRunOptions()

    def test_force_is_kept(self) -> None:
        assert parse_sales_op_options(force=True, months=None) == SalesOpRunOptions(force=True)

    def test_months_list_is_validated_and_deduplicated(self) -> None:
        options = parse_sales_op_options(force=True, months="2026-02, 2026-01,2026-02")

        assert options == SalesOpRunOptions(force=True, months=(date(2026, 2, 1), date(2026, 1, 1)))

    def test_all_months_label_is_case_insensitive(self) -> None:
        assert parse_sales_op_options(force=False, months="ALL") == SalesOpRunOptions(all_months=True)

    @pytest.mark.parametrize("months", ["", "2026-13", "2026-1", "2026-01-01", "all,2026-01", "0000-01"])
    def test_invalid_months_are_rejected(self, months: str) -> None:
        with pytest.raises(ValueError, match="--months"):
            parse_sales_op_options(force=False, months=months)


class TestMonthFolders:
    def test_missing_requested_month_folder_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SalesOpError, match="папка месяца не найдена"):
            _select_month_folders(
                tmp_path,
                options=SalesOpRunOptions(months=(date(2026, 1, 1),)),
                timezone=UTC,
            )

    def test_all_months_lists_existing_month_folders(self, tmp_path: Path) -> None:
        (tmp_path / "2026-07").mkdir()
        (tmp_path / "2026-06").mkdir()
        (tmp_path / "notes").mkdir()

        assert _select_month_folders(
            tmp_path,
            options=SalesOpRunOptions(all_months=True),
            timezone=UTC,
        ) == (date(2026, 6, 1), date(2026, 7, 1))

    def test_all_months_requires_at_least_one_folder(self, tmp_path: Path) -> None:
        with pytest.raises(SalesOpError, match="не найдены папки месяцев"):
            _select_month_folders(
                tmp_path,
                options=SalesOpRunOptions(all_months=True),
                timezone=UTC,
            )

    def test_current_month_comes_from_the_application_timezone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ANN001, ANN206
                return datetime(2026, 2, 15, tzinfo=tz)

        monkeypatch.setattr("sales_op.runner.datetime", _FixedDatetime)
        (tmp_path / "2026-02").mkdir()

        assert _select_month_folders(
            tmp_path,
            options=SalesOpRunOptions(),
            timezone=UTC,
        ) == (date(2026, 2, 1),)


def _settings_with_data_dir(settings: Settings, data_dir: Path) -> Settings:
    module = settings.modules["sales_op"].model_copy(
        update={"sources": settings.modules["sales_op"].sources.model_copy(update={"data_dir": data_dir})},
    )
    modules = dict(settings.modules)
    modules["sales_op"] = module
    return settings.model_copy(update={"modules": modules})


def _source_file(path: Path) -> SourceFile:
    return SourceFile(path=path, display_name=str(path), original_name=path.name)


def test_collect_month_files_skips_underscore_without_force(tmp_path: Path, settings: Settings) -> None:
    month_dir = tmp_path / "2026-07"
    month_dir.mkdir()
    (month_dir / "plan.xlsx").write_bytes(b"")
    (month_dir / "_processed.xlsx").write_bytes(b"")
    test_settings = _settings_with_data_dir(settings, tmp_path)
    module = test_settings.modules["sales_op"]

    without_force = _collect_month_files(
        test_settings,
        module=module,
        month_folders=(date(2026, 7, 1),),
        force=False,
    )
    assert [source.path.name for _month, files in without_force for source in files] == ["plan.xlsx"]

    with_force = _collect_month_files(
        test_settings,
        module=module,
        month_folders=(date(2026, 7, 1),),
        force=True,
    )
    assert [source.path.name for _month, files in with_force for source in files] == ["_processed.xlsx", "plan.xlsx"]


def test_month_mismatch_blocks_only_that_source_group(tmp_path: Path) -> None:
    specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)
    month_start = date(2026, 7, 1)
    good_path = tmp_path / "МФК_Поклиентный план_Рейтинг_июль.xlsx"
    wrong_path = tmp_path / "БСК_Полимеры_Поклиентный план_июль.xlsx"
    _synthetic_plan(good_path, "2026-07-01")
    _synthetic_plan(wrong_path, "2026-08-01")

    selection = _select_source_files(
        ((month_start, (_source_file(good_path), _source_file(wrong_path))),),
        specification_sources=specification.sources,
        timezone=UTC,
    )

    assert set(selection.files_by_group) == {("mfk", month_start)}
    assert selection.failed_groups == frozenset({("bsk_polymers", month_start)})
    assert selection.failures == 1


def test_parse_failure_of_one_file_blocks_the_whole_source_month(tmp_path: Path) -> None:
    specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)
    month_start = date(2026, 7, 1)
    good_path = tmp_path / "БСК_Полимеры_план_01.07.xlsx"
    broken_path = tmp_path / "БСК_Полимеры_план_02.07.xlsx"
    _synthetic_plan(good_path, "2026-07-01")
    _plan_without_headers(broken_path, "2026-07-02")

    selection = _select_source_files(
        ((month_start, (_source_file(good_path), _source_file(broken_path))),),
        specification_sources=specification.sources,
        timezone=UTC,
    )
    parsed = _parse_selected_files(
        selection.files_by_group,
        failed_groups=selection.failed_groups,
        specification_sources=specification.sources,
    )

    assert selection.files_by_group[("bsk_polymers", month_start)]
    assert parsed.rows_by_group == {}
    assert parsed.failures == 1


def test_sales_op_table_gets_created_at_with_a_database_default(settings: Settings) -> None:
    specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)

    table = build_sales_op_table(
        table_reference=settings.modules["sales_op"].table_reference("sales_op_mfk"),
        columns=specification.sources["mfk"].table_columns,
    )

    created_at = table.c["created_at"]
    assert created_at.nullable is False
    assert created_at.server_default is not None
    default_argument = getattr(created_at.server_default, "arg", None)
    assert str(default_argument) == "now()"


def test_view_projection_exposes_created_at_last(settings: Settings) -> None:
    specification = read_sales_op_specification(MOCK_SALES_OP_DIR / SPECIFICATION_FILE_NAME)

    select = _source_select(settings, specification, source_code="mfk")

    assert '"created_at" AS "created_at"\nFROM "el"."sales_op_mfk"' in select


def test_next_month_start_crosses_the_year_boundary() -> None:
    assert _next_month_start(date(2026, 7, 1)) == date(2026, 8, 1)
    assert _next_month_start(date(2026, 12, 1)) == date(2027, 1, 1)
