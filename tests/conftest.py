"""Shared fixtures and helpers for excel_loader tests."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from config import MODULE_CODES, Settings
from dictionary_models import WorkbookMetadata

PROJECT_DIR = Path(__file__).resolve().parents[1]
MOCK_DATA_DIR = PROJECT_DIR / "mock_data"
TEST_SCHEMA = "el"


@pytest.fixture
def settings() -> Settings:
    """
    Build a `Settings` equal to a minimal `config.toml`, without reading the file.

    CI has no `config.toml`; the loader tests exercise parsers over
    `mock_data/` and need only `app_timezone` and the module table
    references (schema `el`), not the real PostgreSQL/IMAP settings.
    `test_config.py` stays the only place that tests the real
    `load_settings()`.
    """
    return Settings.model_validate(
        {
            "common": {"timezone": "UTC"},
            **{code: {"schema": TEST_SCHEMA, "sources": {"data_dir": "mock_data"}} for code in MODULE_CODES},
        }
    )


def workbook_metadata(
    *,
    file_modifier: str = "tester",
    moment: datetime = datetime(2026, 7, 25, tzinfo=UTC),
) -> WorkbookMetadata:
    """Build WorkbookMetadata with fixed timestamps for deterministic tests."""
    return WorkbookMetadata(
        file_modifier=file_modifier,
        file_modified_at=moment,
        loaded_at=moment,
    )
