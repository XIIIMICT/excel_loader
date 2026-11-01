"""
Lock in the A2/A3 contracts from the November 2026 follow-up audit.

A3: ``SourceFileNotFoundError`` is a ``SourceError`` subclass (not a
``BusinessLoadError``) raised by ``_resolve_folder_source`` for a missing file,
so the runner's WARNING+skip path is type-driven and never matches the
Russian text ``"файл не найден"``.

A2: every runner returns a ``ModuleOutcome`` carrying its own
``tables_to_grant`` / ``relation_references_to_grant``.

A6: the per-row order of ``build_dictionary_payload`` is frozen —
``where`` filter → raw skip → convert → converted skip → primary-key duplicate.
"""

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from business_errors import BusinessLoadError, DuplicateCellValueError
from dictionary_models import DbValue, DictionaryPayload, WorkbookMetadata
from excel_parser import CellValueConverter, build_dictionary_payload
from module_models import ModuleOutcome
from source_resolver import (
    SourceAuthError,
    SourceError,
    SourceFileNotFoundError,
    _resolve_folder_source,
)


def test_source_file_not_found_error_is_a_source_error_not_a_business_error() -> None:
    """A missing source file is a WARNING+skip (SourceError), not a data error."""
    error = SourceFileNotFoundError(Path("/data/missing.xlsb"))

    assert isinstance(error, SourceError)
    assert not isinstance(error, BusinessLoadError)
    assert isinstance(SourceAuthError("x"), SourceError)
    assert error.source_path == Path("/data/missing.xlsb")


def test_resolve_folder_source_raises_source_file_not_found_error_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SourceFileNotFoundError):
        _resolve_folder_source(data_dir=tmp_path, pattern_or_path="absent.xlsb")


def test_module_outcome_carries_grant_targets() -> None:
    outcome = ModuleOutcome(
        has_errors=False,
        has_warnings=True,
        has_format_changes=False,
        tables_to_grant=(),
        relation_references_to_grant=("raw.pd_view",),
    )

    assert outcome.has_warnings is True
    assert outcome.relation_references_to_grant == ("raw.pd_view",)


# --- A6: the frozen per-row order of build_dictionary_payload -------------


def _metadata() -> WorkbookMetadata:
    return WorkbookMetadata(
        file_modifier="tester",
        file_modified_at=datetime(2026, 7, 25, tzinfo=UTC),
        loaded_at=datetime(2026, 7, 25, tzinfo=UTC),
    )


def _row_order_frame() -> pd.DataFrame:
    """
    Five rows tuned so each row exercises one stage of the per-row pipeline.

    Columns: where flag, filtered column, required column, primary key,
    unique column (in conversion order, so the primary-key converter is
    observed before a later unique-column duplicate can raise).
    """
    return pd.DataFrame(
        {
            "where_flag": ["keep", "skip", "keep", "keep", "keep"],
            "filtered": ["v1", "skip", "-", "#N/A", "v4"],
            "required": ["r1", "", "r3", "r4", "r5"],
            "key": ["k1", "skip", "k3", "k1", "k2"],
            "uniq": ["u1", "skip", "", "", "u1"],
        },
    )


def _recording_converter(converted: list[str]) -> CellValueConverter:
    def converter(value: object) -> DbValue:
        converted.append(str(value))
        return str(value)

    return converter


def _row_order_payload(converted: list[str]) -> DictionaryPayload:
    return build_dictionary_payload(
        table_reference="raw.order",
        selected_frame=_row_order_frame(),
        primary_key_columns=("key",),
        metadata=_metadata(),
        not_null_columns=("required",),
        not_null_filtered_columns=("filtered",),
        unique_columns=("uniq",),
        column_converters={"key": _recording_converter(converted)},
        where_columns={"where_flag": "keep"},
    )


def test_build_dictionary_payload_row_order_is_frozen() -> None:
    """
    The order where → raw skip → convert → converted skip → PK dup holds.

    * row 2 fails ``where`` and carries an empty required cell — if
      conversion ran anyway, a required-cell error would surface at the end;
    * row 3 (``filtered='-'``) is dropped on the raw skip, before conversion;
    * row 4 converts an Excel-error filtered cell to NULL, is dropped by the
      converted skip, and shares its primary key with row 1 — the
      primary-key check must not run for it;
    * row 5 duplicates row 1 on the unique column — the unique-column
      duplicate wins during conversion, before the primary-key check.
    """
    converted: list[str] = []
    with pytest.raises(DuplicateCellValueError):
        _row_order_payload(converted)

    assert converted == ["k1", "k1", "k2"]


def test_build_dictionary_payload_converted_skip_drops_excel_error_cell_as_warning() -> None:
    """Without the duplicate row the run succeeds: rows 3 stays raw-skipped, row 4 converted-skipped."""
    converted: list[str] = []
    frame = _row_order_frame().iloc[:4].reset_index(drop=True)
    payload = build_dictionary_payload(
        table_reference="raw.order",
        selected_frame=frame,
        primary_key_columns=("key",),
        metadata=_metadata(),
        not_null_columns=("required",),
        not_null_filtered_columns=("filtered",),
        unique_columns=("uniq",),
        column_converters={"key": _recording_converter(converted)},
        where_columns={"where_flag": "keep"},
    )

    assert [row["key"] for row in payload.rows] == ["k1"]
    assert payload.warning_count == 1, "the excel-error value '#N/A' in row 4 is counted as a warning"
    assert converted == ["k1", "k1"], "row 3 must be skipped before conversion, row 4 must be converted then dropped"
