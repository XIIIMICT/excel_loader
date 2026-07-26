from typing import TYPE_CHECKING

from excel_parser import build_dictionary_payload, read_single_sheet_frame

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings
    from dictionary_models import DictionaryPayload

SHEET_NAME = "base"
TABLE_REFERENCE = "raw.quotes_excel_daily"


def build_payload(settings: Settings, source_path: Path) -> DictionaryPayload:
    frame, metadata = read_single_sheet_frame(
        source_path,
        timezone=settings.app_timezone,
        sheet_name=SHEET_NAME,
    )
    return build_dictionary_payload(
        table_reference=TABLE_REFERENCE,
        selected_frame=frame,
        primary_key_columns=(),
        column_kinds={"date": "date", "price": "float"},
        metadata=metadata,
        source_name=str(source_path),
    )
