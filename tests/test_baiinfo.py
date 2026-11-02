from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from business_errors import DuplicatePrimaryKeyError
from dictionaries.quotes import baiinfo

if TYPE_CHECKING:
    from config import Settings

PROJECT_DIR = Path(__file__).resolve().parents[1]
SOURCE_PATH = PROJECT_DIR / "mock_data" / "quotes" / "Baiinfo" / "Baiinfo. Уротропин.xlsb"
SECOND_SOURCE_PATH = PROJECT_DIR / "mock_data" / "quotes" / "Baiinfo" / "Baiinfo. Меламин.xlsb"


def test_builds_baiinfo_payload_with_typed_columns(settings: Settings) -> None:
    payload = baiinfo.build_payload(settings, (SOURCE_PATH,)).payload

    assert payload.table_reference == "el.quotes_excel_baiinfo"
    assert payload.primary_key_columns == ("pub_date", "prod_version")
    assert {column.name: column.kind for column in payload.columns} == {
        "pub_date": "date",
        "prod_version": "text",
        "avg_price": "float",
    }
    assert all(column.nullable is False for column in payload.columns)
    assert payload.rows[0]["pub_date"] == date(2022, 1, 4)
    assert payload.rows[0]["prod_version"] == "Urotropine Average Market Price (Domestic China)"
    assert payload.rows[0]["avg_price"] == 869.46


def test_same_date_across_different_products_is_allowed(settings: Settings) -> None:
    payload = baiinfo.build_payload(settings, (SOURCE_PATH, SECOND_SOURCE_PATH)).payload

    prod_versions = {row["prod_version"] for row in payload.rows}
    assert len(payload.rows) == 2
    assert prod_versions == {
        "Urotropine Average Market Price (Domestic China)",
        "Melamine Average Market Price (Domestic China)",
    }


def test_duplicate_product_and_date_raise_integrity_error(settings: Settings) -> None:
    with pytest.raises(DuplicatePrimaryKeyError):
        baiinfo.build_payload(settings, (SOURCE_PATH, SOURCE_PATH))
