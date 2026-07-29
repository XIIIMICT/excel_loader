from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from business_errors import DuplicatePrimaryKeyError
from config import load_settings
from dictionaries.quotes import sci

PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_duplicate_sci_primary_key_is_an_error() -> None:
    source_paths = tuple(sorted((PROJECT_DIR / "mock_data" / "sci").glob("*.XLSX")))

    with pytest.raises(DuplicatePrimaryKeyError):
        sci.build_payload(load_settings(), source_paths)


def test_builds_sci_payload_with_required_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    source_path = PROJECT_DIR / "mock_data" / "sci" / "SCI СКС_new.example.XLSX"
    source_frame = pd.DataFrame(
        {
            "pub_date": [date(2026, 7, 1)],
            "prod_version": ["SBR 1502 CIF China, USD/т"],
            "avg_price": [1234.5],
            "product_id": ["  product  1  "],
        },
    )
    monkeypatch.setattr(sci, "_read_source_frames", lambda _source_path: [source_frame])

    result = sci.build_payload(load_settings(), (source_path,))
    payload = result.payload
    columns = {column.name: column for column in payload.columns}

    assert payload.table_reference == "el.quotes_excel_sci"
    assert payload.primary_key_columns == ("pub_date", "prod_version", "product_id")
    assert all(columns[column_name].nullable is False for column_name in sci.TARGET_COLUMNS)
    assert payload.rows[0]["product_id"] == "product 1"
