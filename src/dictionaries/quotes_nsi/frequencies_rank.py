from typing import TYPE_CHECKING

from excel_parser import build_dictionary_payload, read_single_sheet_frame

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings
    from dictionary_models import DictionaryPayload

PRIMARY_KEY_COLUMNS = ("frequency_name_ru",)
TABLE_NAME = "quotes_nsi_frequencies_rank"


def build_payload(settings: Settings, source_path: Path) -> DictionaryPayload:
    frame, metadata = read_single_sheet_frame(source_path, timezone=settings.app_timezone)
    return build_dictionary_payload(
        table_reference=settings.quotes_nsi_table_schema_reference(TABLE_NAME),
        selected_frame=frame,
        primary_key_columns=PRIMARY_KEY_COLUMNS,
        column_kinds={"frequency_rank": "int", "rank": "int"},
        metadata=metadata,
        source_name=str(source_path),
        preserve_text_values=True,
    )
