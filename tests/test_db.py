"""
`db.postgres_session` — the A4 engine-lifecycle contract.

The context manager must yield the engine created by ``create_postgres_engine``
and dispose it on both the normal and the exception path; the error itself must
propagate to the caller.
"""

import asyncio
from typing import TYPE_CHECKING

import pytest

import config
import db
from db import postgres_session

if TYPE_CHECKING:
    from collections.abc import Callable

MODULE_CODES = ("quotes_nsi", "quotes", "pd", "sales_op")


class _FakeEngine:
    def __init__(self, disposed: list[str]) -> None:
        self._disposed = disposed

    async def dispose(self) -> None:
        self._disposed.append("dispose")


class _BoomError(Exception):
    pass


def _settings() -> config.Settings:
    module = {"schema": "raw", "sources": {"data_dir": "data"}}
    data: dict[str, object] = dict.fromkeys(MODULE_CODES, module)
    return config.Settings.model_validate(data)


def _engine_factory(disposed: list[str]) -> Callable[..., _FakeEngine]:
    def factory(
        _settings: config.Settings,
        *,
        database: str | None = None,  # noqa: ARG001  # keyword-only in the real call signature
        isolation_level: str | None = None,  # noqa: ARG001
    ) -> _FakeEngine:
        return _FakeEngine(disposed)

    return factory


def test_postgres_session_yields_a_created_engine() -> None:
    async def run() -> bool:
        async with postgres_session(_settings()) as engine:
            return engine is not None

    assert asyncio.run(run()) is True


def test_postgres_session_disposes_the_engine_after_a_successful_run(monkeypatch: pytest.MonkeyPatch) -> None:
    disposed: list[str] = []
    monkeypatch.setattr(db, "create_postgres_engine", _engine_factory(disposed))

    async def run() -> bool:
        async with db.postgres_session(_settings()) as engine:
            return isinstance(engine, _FakeEngine)

    assert asyncio.run(run()) is True
    assert disposed == ["dispose"]


def test_postgres_session_disposes_the_engine_on_the_error_path(monkeypatch: pytest.MonkeyPatch) -> None:
    disposed: list[str] = []
    monkeypatch.setattr(db, "create_postgres_engine", _engine_factory(disposed))

    async def run() -> None:
        async with db.postgres_session(_settings()):
            error = _BoomError("boom")
            raise error

    with pytest.raises(_BoomError, match="boom"):
        asyncio.run(run())

    assert disposed == ["dispose"]
