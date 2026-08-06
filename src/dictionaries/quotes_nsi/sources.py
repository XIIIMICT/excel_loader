from __future__ import annotations

from typing import TYPE_CHECKING

from excel_parser import build_dictionary_payload, read_single_sheet_frame

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings
    from dictionary_models import DictionaryPayload

PRIMARY_KEY_COLUMNS = ("source_code",)
TABLE_NAME = "quotes_nsi_sources"
SOURCE_COLUMNS = ("source_code", "source_fullname", "source_shortname")
NOT_NULL_COLUMNS = SOURCE_COLUMNS
UNIQUE_COLUMNS = ("source_code", "source_fullname", "dest_table")


def build_payload(settings: Settings, source_path: Path) -> DictionaryPayload:
    frame, metadata = read_single_sheet_frame(source_path, timezone=settings.app_timezone, sheet_name="dic")
    selected_frame = frame.loc[:, list(SOURCE_COLUMNS)].copy()
    selected_frame["dest_table"] = None
    return build_dictionary_payload(
        table_reference=settings.quotes_nsi_table_schema_reference(TABLE_NAME),
        selected_frame=selected_frame,
        primary_key_columns=PRIMARY_KEY_COLUMNS,
        not_null_columns=NOT_NULL_COLUMNS,
        unique_columns=UNIQUE_COLUMNS,
        metadata=metadata,
        source_name=str(source_path),
    )
