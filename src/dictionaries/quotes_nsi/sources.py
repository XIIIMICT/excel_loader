from __future__ import annotations

from typing import TYPE_CHECKING

from excel_parser import build_dictionary_payload, read_single_sheet_frame

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings
    from dictionary_models import DictionaryPayload

PRIMARY_KEY_COLUMNS = ("source_code",)
TABLE_NAME = "quotes_nsi_sources"
COLUMNS = ("source_code", "source_fullname", "source_shortname")


def build_payload(settings: Settings, source_path: Path) -> DictionaryPayload:
    frame, metadata = read_single_sheet_frame(source_path, timezone=settings.app_timezone, sheet_name="dic")
    return build_dictionary_payload(
        table_reference=settings.quotes_nsi_table_schema_reference(TABLE_NAME),
        selected_frame=frame.loc[:, list(COLUMNS)].copy(),
        primary_key_columns=PRIMARY_KEY_COLUMNS,
        not_null_columns=COLUMNS,
        unique_columns=COLUMNS,
        metadata=metadata,
        source_name=str(source_path),
    )
