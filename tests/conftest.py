"""Shared fixtures and helpers for excel_loader tests."""

from datetime import UTC, datetime
from pathlib import Path

from dictionary_models import WorkbookMetadata

PROJECT_DIR = Path(__file__).resolve().parents[1]
MOCK_DATA_DIR = PROJECT_DIR / "mock_data"


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
