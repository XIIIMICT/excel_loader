from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from dictionaries.quotes.daily_excel import build_payload

if TYPE_CHECKING:
    from config import Settings

PROJECT_DIR = Path(__file__).resolve().parents[1]
SOURCE_PATH = PROJECT_DIR / "mock_data" / "quotes" / "Daily Quotes" / "Ежедневные котировки.xlsb"


def test_builds_typed_daily_quotes_payload_from_base_sheet(settings: Settings) -> None:
    payload = build_payload(settings, SOURCE_PATH)

    assert payload.table_reference == "raw.quotes_excel_daily"
    assert {column.name: column.kind for column in payload.columns} == {
        "pub_date": "date",
        "prod_version": "text",
        "avg_price": "float",
    }
    assert payload.rows[0]["pub_date"] == date(2009, 1, 1)
    assert payload.rows[0]["prod_version"] == "Текст"
    assert payload.rows[0]["avg_price"] == 14.88
    assert payload.primary_key_columns == ()
