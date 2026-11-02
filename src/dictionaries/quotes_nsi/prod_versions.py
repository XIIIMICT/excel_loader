from typing import TYPE_CHECKING

from excel_parser import build_dictionary_payload, read_single_sheet_frame, select_columns_between

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings
    from dictionary_models import DictionaryPayload

PRIMARY_KEY_COLUMNS = ("prod_version", "source_code")
TABLE_NAME = "quotes_nsi_prod_versions"


def build_payload(settings: Settings, source_path: Path) -> DictionaryPayload:
    frame, metadata = read_single_sheet_frame(source_path, timezone=settings.app_timezone)
    selected_frame = select_columns_between(
        frame,
        start_column="prod_version",
        end_column="source_code",
    )
    return build_dictionary_payload(
        table_reference=settings.modules["quotes_nsi"].table_reference(TABLE_NAME),
        selected_frame=selected_frame,
        primary_key_columns=PRIMARY_KEY_COLUMNS,
        column_kinds={"inserted": "datetime", "is_top_raw_quotes": "bool"},
        not_null_columns=("is_top_raw_quotes",),
        metadata=metadata,
        source_name=str(source_path),
    )
