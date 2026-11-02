from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

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


@asynccontextmanager
async def postgres_session(
    settings: Settings,
    *,
    database: str | None = None,
    isolation_level: str | None = None,
) -> AsyncGenerator[AsyncEngine]:
    """
    Single owner of the engine lifecycle for one run segment.

    Creates one engine, yields it, and disposes it on both normal and
    exception paths. Every runner and post-action uses this instead of a
    hand-rolled ``engine = create_postgres_engine(...); try/finally: dispose``
    so the cleanup path exists once.
    """
    engine = create_postgres_engine(settings, database=database, isolation_level=isolation_level)
    try:
        yield engine
    finally:
        await engine.dispose()
