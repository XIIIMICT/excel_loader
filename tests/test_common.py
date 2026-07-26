from datetime import UTC, datetime

import pandas as pd

from dictionary_models import WorkbookMetadata
from excel_parser import build_dictionary_payload
from main import _filter_email_log_content


def test_payload_without_primary_key_accepts_multiple_rows() -> None:
    payload = build_dictionary_payload(
        table_reference="raw.example",
        selected_frame=pd.DataFrame({"value": ["один", "два"]}),
        primary_key_columns=(),
        metadata=WorkbookMetadata(
            file_modifier="user",
            file_modified_at=datetime(2026, 7, 25, tzinfo=UTC),
            loaded_at=datetime(2026, 7, 25, tzinfo=UTC),
        ),
    )

    assert [row["value"] for row in payload.rows] == ["один", "два"]


def test_email_log_contains_only_warning_and_errors() -> None:
    content = (
        '{"level": "INFO", "message": "готово"}\n'
        '{"level": "WARNING", "message": "предупреждение"}\n'
        '{"level": "ERROR", "message": "ошибка"}'
    )

    filtered = _filter_email_log_content(content)

    assert "готово" not in filtered
    assert "предупреждение" in filtered
    assert "ошибка" in filtered
