from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from business_errors import DuplicateCellValueError, RequiredCellValueError
from config import load_settings
from excel_parser import build_dictionary_payload, frame_origin, read_single_sheet_frame, select_columns_between

if TYPE_CHECKING:
    import pandas as pd

    from dictionary_models import WorkbookMetadata

PROJECT_DIR = Path(__file__).resolve().parents[1]
SOURCE_PATH = PROJECT_DIR / "mock_data" / "quotes_nsi" / "quotes_nsi_argus_fc_exception.xlsb"


def _selected_frame() -> tuple[pd.DataFrame, WorkbookMetadata]:
    settings = load_settings()
    frame, metadata = read_single_sheet_frame(SOURCE_PATH, timezone=settings.app_timezone)
    selected = select_columns_between(
        frame,
        start_column="source_code",
        end_column="is_change_scenario_to_fact",
    )
    return selected.copy(), metadata


def test_frame_origin_reports_real_sheet_and_column_letters() -> None:
    frame, _ = read_single_sheet_frame(SOURCE_PATH, timezone=load_settings().app_timezone)

    origin = frame_origin(frame)

    assert origin is not None
    assert origin.sheet_name == "dic"
    assert origin.column_letters["source_code"] == "A"
    assert origin.column_letters["prod_version"] == "B"


def test_required_error_keeps_sheet_name_and_original_cell() -> None:
    frame, metadata = _selected_frame()
    frame.loc[0, "source_code"] = None

    with pytest.raises(RequiredCellValueError) as error_info:
        build_dictionary_payload(
            table_reference="el.example",
            selected_frame=frame,
            primary_key_columns=("source_code",),
            metadata=metadata,
            source_name="quotes_nsi_argus_fc_exception.xlsb",
        )

    assert "dic!A2" in error_info.value.reason
    assert "quotes_nsi_argus_fc_exception.xlsb" in error_info.value.reason


def test_unique_error_keeps_sheet_name_and_original_cells() -> None:
    frame, metadata = _selected_frame()
    frame.loc[0, "prod_version"] = "same"
    frame.loc[1, "prod_version"] = "same"

    with pytest.raises(DuplicateCellValueError) as error_info:
        build_dictionary_payload(
            table_reference="el.example",
            selected_frame=frame,
            primary_key_columns=(),
            unique_columns=("prod_version",),
            metadata=metadata,
        )

    assert "dic!B2" in error_info.value.reason
    assert "dic!B3" in error_info.value.reason
