from datetime import date
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from openpyxl import Workbook

from business_errors import DuplicatePrimaryKeyError
from dictionaries.quotes import sci

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings


def _new_sci_workbook() -> Workbook:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "SBR1502 East China Mrkt Price"
    sheet["D3"] = "product 1"
    sheet["A9"] = "Дата"
    sheet["B9"] = "Товар"
    sheet["D9"] = "Марка"
    sheet["P9"] = "CIF China, $/т"
    sheet["A10"], sheet["B10"], sheet["D10"], sheet["P10"] = date(2005, 5, 5), "SBR", 1488, 100.5
    return workbook


@pytest.fixture
def sci_source_path(tmp_path: Path) -> Path:
    path = tmp_path / "SCI СКС_new.XLSX"
    _new_sci_workbook().save(path)
    return path


def test_skips_rows_without_a_product_and_reads_past_aggregate_rows(
    settings: Settings,
    sci_source_path: Path,
) -> None:
    workbook = _new_sci_workbook()
    sheet = workbook.active
    # An aggregate row with a date but no product (only calculated prices).
    sheet["A11"], sheet["H11"], sheet["P11"] = date(2023, 1, 1), 11200, 110.5
    # A monthly aggregate row with a month name instead of a date.
    sheet["A12"], sheet["H12"], sheet["P12"] = "Февраль", 11800, 120.5
    # A valid quote row after the aggregates: must still be read.
    sheet["A13"], sheet["B13"], sheet["D13"], sheet["P13"] = date(2013, 8, 1), "SBR", 1488, 200.5
    workbook.save(sci_source_path)

    payload = sci.build_payload(settings, (sci_source_path,)).payload

    assert [row["pub_date"] for row in payload.rows] == [date(2005, 5, 5), date(2013, 8, 1)]
    assert [row["avg_price"] for row in payload.rows] == [100.5, 200.5]


def test_fully_blank_row_ends_the_data_block(settings: Settings, sci_source_path: Path) -> None:
    workbook = _new_sci_workbook()
    sheet = workbook.active
    # Row 11 is fully empty -> stop. Row 12 has a product but must not be read.
    sheet["A12"], sheet["B12"], sheet["D12"], sheet["P12"] = date(2013, 8, 1), "SBR", 1488, 200.5
    workbook.save(sci_source_path)

    payload = sci.build_payload(settings, (sci_source_path,)).payload

    assert len(payload.rows) == 1
    assert payload.rows[0]["pub_date"] == date(2005, 5, 5)
    assert payload.rows[0]["avg_price"] == 100.5


def test_duplicate_sci_primary_key_is_an_error(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    sci_source_path: Path,
) -> None:
    source_frame = pd.DataFrame(
        {
            "Дата": [date(2026, 7, 1), date(2026, 7, 1)],
            "prod_version": ["SBR 1502 CIF China, USD/т", "SBR 1502 CIF China, USD/т"],
            "CIF China, $/т": [1234.5, 1234.5],
            "product_id": ["product 1", "product 1"],
        },
    )
    monkeypatch.setattr(sci, "_read_source_frames", lambda _source_path: ([source_frame], []))

    with pytest.raises(DuplicatePrimaryKeyError):
        sci.build_payload(settings, (sci_source_path,))


def test_builds_sci_payload_with_required_columns(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    sci_source_path: Path,
) -> None:
    source_frame = pd.DataFrame(
        {
            "Дата": [date(2026, 7, 1)],
            "prod_version": ["SBR 1502 CIF China, USD/т"],
            "CIF China, $/т": [1234.5],
            "product_id": ["  product  1  "],
        },
    )
    monkeypatch.setattr(sci, "_read_source_frames", lambda _source_path: ([source_frame], []))

    result = sci.build_payload(settings, (sci_source_path,))
    payload = result.payload
    columns = {column.name: column for column in payload.columns}

    assert payload.table_reference == "el.quotes_excel_sci"
    assert payload.primary_key_columns == ("pub_date", "prod_version", "product_id")
    assert all(columns[column_name].nullable is False for column_name in sci.TARGET_COLUMNS)
    assert payload.rows[0]["product_id"] == "product 1"
