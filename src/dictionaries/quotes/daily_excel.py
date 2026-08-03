from typing import TYPE_CHECKING

from excel_parser import build_dictionary_payload, read_single_sheet_frame

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings
    from dictionary_models import DictionaryPayload

SHEET_NAME = "base"
TABLE_REFERENCE = "raw.quotes_excel_daily"
SOURCE_COLUMN_ALIASES = {
    "Дата": "pub_date",
    "Продукт": "prod_version",
    "Цена": "avg_price",
}


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
        not_null_columns=("prod_version",),
        column_kinds={"pub_date": "date", "avg_price": "float"},
        metadata=metadata,
        source_name=str(source_path),
        column_aliases=SOURCE_COLUMN_ALIASES,
    )
