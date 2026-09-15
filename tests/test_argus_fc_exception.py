from pathlib import Path

from config import load_settings
from dictionaries.quotes_nsi.argus_fc_exception import build_payload

PROJECT_DIR = Path(__file__).resolve().parents[1]
SOURCE_PATH = PROJECT_DIR / "mock_data" / "quotes_nsi" / "quotes_nsi_argus_fc_exception.xlsb"


def test_builds_argus_fc_exception_payload_with_boolean_columns() -> None:
    payload = build_payload(load_settings(), SOURCE_PATH)

    columns = {column.name: column for column in payload.columns}

    assert payload.table_reference == "el.quotes_nsi_argus_fc_exception"
    assert payload.primary_key_columns == ("source_code", "prod_version")
    assert {column.name: column.kind for column in payload.columns} == {
        "source_code": "text",
        "prod_version": "text",
        "is_change_report_date_to_pubdate": "bool",
        "is_change_scenario_to_fact": "bool",
    }
    assert columns["source_code"].nullable is False
    assert columns["prod_version"].nullable is False
    assert columns["is_change_report_date_to_pubdate"].nullable is True
    assert columns["is_change_scenario_to_fact"].nullable is True
    assert payload.rows
    assert all(row["is_change_report_date_to_pubdate"] is True for row in payload.rows)
    assert all(row["is_change_scenario_to_fact"] is True for row in payload.rows)
