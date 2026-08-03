from datetime import UTC, date, datetime

import pandas as pd
import pytest

from business_errors import (
    DatabaseOperationError,
    DuplicateCellValueError,
    RequiredCellValueError,
    SourceColumnMissingError,
)
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


def test_column_aliases_map_source_to_target_names() -> None:
    payload = build_dictionary_payload(
        table_reference="el.example",
        selected_frame=pd.DataFrame(
            {
                "Дата": [date(2026, 7, 1), date(2026, 7, 2)],
                "Продукт": ["А", "Б"],
                "Цена": [1.5, 2.5],
            },
        ),
        primary_key_columns=("pub_date",),
        not_null_columns=("prod_version",),
        column_kinds={"pub_date": "date", "avg_price": "float"},
        metadata=_metadata(),
        column_aliases={"Дата": "pub_date", "Продукт": "prod_version", "Цена": "avg_price"},
    )

    columns = {column.name: column for column in payload.columns}
    assert set(columns) == {"pub_date", "prod_version", "avg_price"}
    assert columns["pub_date"].kind == "date"
    assert columns["avg_price"].kind == "float"
    assert columns["prod_version"].nullable is False
    assert payload.rows[0]["pub_date"] == date(2026, 7, 1)
    assert payload.rows[-1]["avg_price"] == 2.5


def test_column_aliases_missing_source_column_raises() -> None:
    with pytest.raises(SourceColumnMissingError):
        build_dictionary_payload(
            table_reference="el.example",
            selected_frame=pd.DataFrame({"Продукт": ["А"]}),
            primary_key_columns=(),
            metadata=_metadata(),
            column_aliases={"Отсутствует": "value"},
        )


def test_column_aliases_duplicate_target_names_raise() -> None:
    with pytest.raises(ValueError, match="дублированию"):
        build_dictionary_payload(
            table_reference="el.example",
            selected_frame=pd.DataFrame({"Дата": [1], "Цена": [2]}),
            primary_key_columns=(),
            metadata=_metadata(),
            column_aliases={"Дата": "value", "Цена": "value"},
        )


def test_where_columns_filter_rows_and_dropped_columns_are_removed() -> None:
    payload = build_dictionary_payload(
        table_reference="el.example",
        selected_frame=pd.DataFrame(
            {
                "date": [date(2022, 1, 4), date(2022, 1, 5)],
                "price": [1.5, 2.5],
                "unit": ["USD/T", "EUR/T"],
            },
        ),
        primary_key_columns=("date",),
        not_null_columns=("price",),
        column_kinds={"date": "date", "price": "float"},
        metadata=_metadata(),
        where_columns={"unit": "USD/T"},
        dropped_columns=("unit",),
    )

    assert {column.name for column in payload.columns} == {"date", "price"}
    assert [row["price"] for row in payload.rows] == [1.5]


def test_custom_column_converter_is_applied() -> None:
    payload = build_dictionary_payload(
        table_reference="el.example",
        selected_frame=pd.DataFrame({"date_text": ["04-Jan-22"], "price": [1.5]}),
        primary_key_columns=("date",),
        not_null_columns=("price",),
        column_kinds={"price": "float"},
        metadata=_metadata(),
        column_aliases={"date_text": "date"},
        column_converters={"date": lambda value: date.strptime(str(value), "%d-%b-%y")},
    )

    assert payload.rows[0]["date"] == date(2022, 1, 4)
    assert payload.rows[0]["price"] == 1.5


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
