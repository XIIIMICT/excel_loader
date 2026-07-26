from typing import TYPE_CHECKING

from excel_parser import build_dictionary_payload, read_single_sheet_frame, select_columns_between

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings
    from dictionary_models import DictionaryPayload

PRIMARY_KEY_COLUMNS = ("rep_date", "prod_version")
TABLE_NAME = "quotes_cheminsight_p"


def build_payload(settings: Settings, source_path: Path) -> DictionaryPayload:
    frame, metadata = read_single_sheet_frame(source_path, timezone=settings.app_timezone)
    selected_frame = select_columns_between(
        frame,
        start_column="rep_date",
        end_column="prod_version",
    )
    return build_dictionary_payload(
        table_reference=settings.quotes_table_schema_reference(TABLE_NAME),
        selected_frame=selected_frame,
        primary_key_columns=PRIMARY_KEY_COLUMNS,
        not_null_filtered_columns=("min_price", "max_price"),
        column_kinds={
            "rep_date": "date",
            "min_price": "float",
            "max_price": "float",
            "avg_price": "float",
        },
        metadata=metadata,
        source_name=str(source_path),
    )
