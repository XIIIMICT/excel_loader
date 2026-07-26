from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Column, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP

from storage import prepare_storage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

HISTORY_SCHEMA = "el"
HISTORY_TABLE_NAME = "excel_loader_sources_history"


def build_source_history_table() -> Table:
    metadata = MetaData(schema=HISTORY_SCHEMA)
    return Table(
        HISTORY_TABLE_NAME,
        metadata,
        Column("loaded_at", TIMESTAMP(timezone=True), nullable=False, comment="Дата и время загрузки файла."),
        Column("src_file", Text, nullable=False, comment="Путь к исходному файлу или IMAP-вложению."),
        Column("dest_table", Text, nullable=False, comment="Целевая таблица загрузки."),
        Column(
            "file_modified_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            comment="Дата и время последнего изменения исходного файла.",
        ),
        Column("file_modifier", Text, nullable=False, comment="Пользователь, изменивший исходный файл."),
    )


async def prepare_source_history(engine: AsyncEngine, *, users_to_grant: tuple[str, ...]) -> Table:
    table = build_source_history_table()
    await prepare_storage(table, engine=engine, users_to_grant=users_to_grant)
    return table
