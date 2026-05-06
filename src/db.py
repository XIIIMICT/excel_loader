from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if TYPE_CHECKING:
    from config import Settings


def create_postgres_engine(
    settings: Settings,
    *,
    database: str | None = None,
    isolation_level: str | None = None,
) -> AsyncEngine:
    return create_async_engine(
        settings.sqlalchemy_url(database=database),
        isolation_level=isolation_level,
        pool_pre_ping=True,
    )
