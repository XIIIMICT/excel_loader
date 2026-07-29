from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from config import load_settings
from dictionaries.quotes_nsi.sources import build_payload as build_sources_payload
from dictionary_models import WorkbookMetadata
from excel_parser import build_dictionary_payload

PROJECT_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("value", "nullable_mode", "expected"),
    [
        (None, "nullable", None),
        (None, "required", False),
        (True, "required", True),
        (False, "required", False),
        (0, "required", False),
        (2, "required", True),
        (" false ", "required", False),
        ("", "nullable", None),
        ("", "required", False),
        ("0", "required", True),
        ("да", "required", True),
    ],
)
def test_boolean_conversion(value: object, nullable_mode: str, expected: object) -> None:
    not_null_columns = ("flag",) if nullable_mode == "required" else ()
    payload = build_dictionary_payload(
        table_reference="el.flags",
        selected_frame=pd.DataFrame({"flag": [value]}),
        primary_key_columns=(),
        not_null_columns=not_null_columns,
        column_kinds={"flag": "bool"},
        metadata=WorkbookMetadata(
            file_modifier="tester",
            file_modified_at=datetime(2026, 7, 29, tzinfo=UTC),
            loaded_at=datetime(2026, 7, 29, tzinfo=UTC),
        ),
    )

    assert payload.rows[0]["flag"] is expected


def test_sources_are_normalized_and_have_database_constraints() -> None:
    source_path = PROJECT_DIR / "mock_data" / "quotes_nsi" / "quotes_nsi_sources.example.xlsx"
    payload = build_sources_payload(load_settings(), source_path)
    columns = {column.name: column for column in payload.columns}

    assert payload.primary_key_columns == ("source_code",)
    assert columns["source_code"].nullable is False
    assert columns["source_code"].unique is True
    assert columns["source_fullname"].nullable is False
    assert columns["source_fullname"].unique is True
    assert columns["source_shortname"].nullable is False
    assert columns["source_shortname"].unique is True
    assert all(
        value == " ".join(value.split()) for row in payload.rows for value in row.values() if isinstance(value, str)
    )
