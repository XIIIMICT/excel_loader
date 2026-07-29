from datetime import UTC, datetime

import pandas as pd
import pytest

from business_errors import DatabaseOperationError, DuplicateCellValueError, RequiredCellValueError
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


def test_required_value_error_contains_cell_and_value() -> None:
    with pytest.raises(RequiredCellValueError, match=r"name.*A2.*None"):
        build_dictionary_payload(
            table_reference="el.example",
            selected_frame=pd.DataFrame({"name": [None]}),
            primary_key_columns=(),
            not_null_columns=("name",),
            metadata=_metadata(),
        )


def test_unique_value_error_contains_both_cells_and_value() -> None:
    with pytest.raises(DuplicateCellValueError, match=r"code.*same.*A2.*A3"):
        build_dictionary_payload(
            table_reference="el.example",
            selected_frame=pd.DataFrame({"code": ["same", "same"]}),
            primary_key_columns=(),
            unique_columns=("code",),
            metadata=_metadata(),
        )


def test_database_error_contains_postgres_diagnostics_without_traceback() -> None:
    class Diagnostic:
        column_name = "rank"
        constraint_name = "quotes_rank_check"
        message_primary = "invalid input syntax for type integer"
        message_detail = "Failing row contains (bad)."

    class OriginalError(Exception):
        diag = Diagnostic()
        sqlstate = "22P02"

    error = DatabaseOperationError.from_dbapi_error(
        OriginalError(),
        operation="изменение типа",
        table_reference="el.quotes",
    )

    assert error.reason == (
        "изменение типа: таблица=el.quotes, столбец=rank, ограничение=quotes_rank_check, SQLSTATE=22P02; "
        "invalid input syntax for type integer; Failing row contains (bad)."
    )


def _metadata() -> WorkbookMetadata:
    return WorkbookMetadata(
        file_modifier="user",
        file_modified_at=datetime(2026, 7, 25, tzinfo=UTC),
        loaded_at=datetime(2026, 7, 25, tzinfo=UTC),
    )
