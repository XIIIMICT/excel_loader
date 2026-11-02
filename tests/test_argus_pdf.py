from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from business_errors import DuplicatePrimaryKeyError
from dictionaries.quotes.argus_pdf import build_payload

if TYPE_CHECKING:
    from config import Settings

PROJECT_DIR = Path(__file__).resolve().parents[1]
SOURCE_PATH = PROJECT_DIR / "mock_data" / "quotes" / "Argus PDF" / "Argus PDF Удобрения.xlsb"


def test_builds_argus_pdf_payload_with_typed_columns(settings: Settings) -> None:
    payload = build_payload(settings, (SOURCE_PATH,)).payload

    assert payload.table_reference == "el.quotes_excel_argus_pdf"
    assert payload.primary_key_columns == ("rep_date", "prod_version")
    assert {column.name: column.kind for column in payload.columns} == {
        "rep_date": "date",
        "prod_version": "text",
        "min_price": "int",
        "avg_price": "float",
        "max_price": "int",
    }
    assert payload.rows[0]["rep_date"] == date(2025, 10, 28)
    assert payload.rows[0]["prod_version"] == "SOP FOB Egypt, USD/t"
    assert payload.rows[0]["min_price"] == 620
    assert payload.rows[0]["avg_price"] == 640.0
    assert payload.rows[0]["max_price"] == 660
    assert payload.rows[1]["rep_date"] == date(2025, 11, 4)


def test_duplicate_rep_date_and_prod_version_raise_integrity_error(settings: Settings) -> None:
    with pytest.raises(DuplicatePrimaryKeyError):
        build_payload(settings, (SOURCE_PATH, SOURCE_PATH))
