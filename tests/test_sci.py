from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from business_errors import DuplicatePrimaryKeyError
from config import load_settings
from dictionaries.quotes import sci

PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_blank_row_ends_the_data_block() -> None:
    source_path = PROJECT_DIR / "mock_data" / "quotes" / "SCI" / "SCI СКС_new.XLSX"

    payload = sci.build_payload(load_settings(), (source_path,)).payload

    # Two real rows; the blank separator row and everything after it (a repeated
    # duplicate row and a total row) are not part of the report.
    assert len(payload.rows) == 2
    assert [row["pub_date"] for row in payload.rows] == [date(2005, 5, 5), date(2007, 8, 1)]


def test_duplicate_sci_primary_key_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source_path = PROJECT_DIR / "mock_data" / "quotes" / "SCI" / "SCI СКС_new.XLSX"
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
        sci.build_payload(load_settings(), (source_path,))


def test_builds_sci_payload_with_required_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    source_path = PROJECT_DIR / "mock_data" / "quotes" / "SCI" / "SCI СКС_new.XLSX"
    source_frame = pd.DataFrame(
        {
            "Дата": [date(2026, 7, 1)],
            "prod_version": ["SBR 1502 CIF China, USD/т"],
            "CIF China, $/т": [1234.5],
            "product_id": ["  product  1  "],
        },
    )
    monkeypatch.setattr(sci, "_read_source_frames", lambda _source_path: ([source_frame], []))

    result = sci.build_payload(load_settings(), (source_path,))
    payload = result.payload
    columns = {column.name: column for column in payload.columns}

    assert payload.table_reference == "el.quotes_excel_sci"
    assert payload.primary_key_columns == ("pub_date", "prod_version", "product_id")
    assert all(columns[column_name].nullable is False for column_name in sci.TARGET_COLUMNS)
    assert payload.rows[0]["product_id"] == "product 1"
