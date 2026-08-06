from typing import TYPE_CHECKING

from excel_parser import build_dictionary_payload, read_single_sheet_frame, select_columns_between

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings
    from dictionary_models import DictionaryPayload

PRIMARY_KEY_COLUMNS = ("source_code", "prod_version")
TABLE_NAME = "quotes_nsi_argus_fc_exception"
BOOLEAN_COLUMNS = ("is_change_report_date_to_pubdate", "is_change_scenario_to_fact")


def build_payload(settings: Settings, source_path: Path) -> DictionaryPayload:
    frame, metadata = read_single_sheet_frame(source_path, timezone=settings.app_timezone)
    selected_frame = select_columns_between(
        frame,
        start_column="source_code",
        end_column="is_change_scenario_to_fact",
    )
    return build_dictionary_payload(
        table_reference=settings.quotes_nsi_table_schema_reference(TABLE_NAME),
        selected_frame=selected_frame,
        primary_key_columns=PRIMARY_KEY_COLUMNS,
        column_kinds=dict.fromkeys(BOOLEAN_COLUMNS, "bool"),
        metadata=metadata,
        source_name=str(source_path),
    )
