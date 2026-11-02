"""Workbook metadata: the shared `docProps/core.xml` reader and the `modified_at` semantics."""

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZipFile

import pytest

from business_errors import FileMetadataMissingError
from dictionary_models import WorkbookMetadata
from excel_parser import OOXML_CORE_PROPERTIES_PATH, OOXML_NAMESPACES, read_core_properties, read_workbook_metadata
from sales_op.excel_parser import _workbook_modified_at
from source_resolver import SourceFileNotFoundError

if TYPE_CHECKING:
    from config import Settings

PROJECT_DIR = Path(__file__).resolve().parents[1]
CORE_PROPERTIES_NAMESPACES = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in OOXML_NAMESPACES.items())
FULL_CORE_PROPERTIES_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties {CORE_PROPERTIES_NAMESPACES}>
  <dc:creator>Автор</dc:creator>
  <cp:lastModifiedBy>Правка</cp:lastModifiedBy>
  <dcterms:created>2025-10-28T06:20:00Z</dcterms:created>
  <dcterms:modified>2026-07-25T13:15:00+03:00</dcterms:modified>
</cp:coreProperties>"""


def _write_package(path: Path, *, core_properties: str | None) -> Path:
    """Write a minimal zip package: the reader only opens it and reads one part."""
    with ZipFile(path, "w") as package:
        package.writestr("xl/workbook.xml", "<workbook/>")
        if core_properties is not None:
            package.writestr(OOXML_CORE_PROPERTIES_PATH, core_properties)
    return path


def _core_xml(modified_element: str, *, with_names: bool = True) -> str:
    creator_element = "  <dc:creator>Автор</dc:creator>\n" if with_names else ""
    last_modified_element = "  <cp:lastModifiedBy>Правка</cp:lastModifiedBy>\n" if with_names else ""
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties {CORE_PROPERTIES_NAMESPACES}>
{creator_element}{last_modified_element}  {modified_element}
</cp:coreProperties>"""


def _metadata() -> WorkbookMetadata:
    return WorkbookMetadata(
        file_modifier="tester",
        file_modified_at=datetime(2026, 1, 1, tzinfo=UTC),
        loaded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_read_core_properties_parses_names_and_timestamps(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "example.xlsx", core_properties=FULL_CORE_PROPERTIES_XML)

    properties = read_core_properties(package)

    assert properties.creator == "Автор"
    assert properties.last_modified_by == "Правка"
    # `Z` and offset forms both parse; equality is instant-based for aware values.
    assert properties.created == datetime(2025, 10, 28, 6, 20, tzinfo=UTC)
    assert properties.modified == datetime(2026, 7, 25, 10, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    ("scenario", "package_name"),
    [
        ("no_core_part", "no_core.xlsx"),
        ("not_a_zip", "text.xlsx"),
        ("unparsed_timestamp", "bad_date.xlsx"),
    ],
)
def test_read_core_properties_raises_when_metadata_is_unreadable(
    tmp_path: Path, scenario: str, package_name: str
) -> None:
    if scenario == "no_core_part":
        source_path = _write_package(tmp_path / package_name, core_properties=None)
    elif scenario == "not_a_zip":
        source_path = tmp_path / package_name
        source_path.write_text("definitely not a workbook", encoding="utf-8")
    else:
        source_path = _write_package(
            tmp_path / package_name,
            core_properties=_core_xml("<dcterms:modified>not a date</dcterms:modified>"),
        )

    with pytest.raises(FileMetadataMissingError):
        read_core_properties(source_path)


def test_read_workbook_metadata_requires_a_user_name(tmp_path: Path, settings: Settings) -> None:
    # Core properties without creator/lastModifiedBy have no source-history owner.
    source_path = _write_package(
        tmp_path / "anonymous.xlsx",
        core_properties=_core_xml("<dcterms:modified>2026-07-25T00:00:00Z</dcterms:modified>", with_names=False),
    )

    with pytest.raises(FileMetadataMissingError):
        read_workbook_metadata(source_path, timezone=settings.app_timezone)


def test_read_workbook_metadata_uses_core_properties_for_xlsx_and_xlsb(settings: Settings) -> None:
    sources = (
        PROJECT_DIR / "mock_data" / "pd" / "pd.xlsx",
        PROJECT_DIR / "mock_data" / "quotes" / "Daily Quotes" / "Ежедневные котировки.xlsb",
    )

    for source_path in sources:
        metadata = read_workbook_metadata(source_path, timezone=settings.app_timezone)

        # Same `docProps/core.xml` name extraction for both suffixes, no openpyxl open.
        assert metadata.file_modifier
        assert metadata.file_modified_at.tzinfo is not None
        assert metadata.loaded_at.tzinfo is not None


def test_read_workbook_metadata_missing_source_raises(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(SourceFileNotFoundError):
        read_workbook_metadata(tmp_path / "absent.xlsx", timezone=settings.app_timezone)


@pytest.mark.parametrize(
    ("modified_element", "expected"),
    [
        (
            "<dcterms:modified>2026-07-25T13:15:00+03:00</dcterms:modified>",
            datetime.fromisoformat("2026-07-25T10:15"),
        ),
        (
            "<dcterms:modified>2025-10-28T06:20:00Z</dcterms:modified>",
            datetime.fromisoformat("2025-10-28T06:20"),
        ),
    ],
)
def test_workbook_modified_at_normalizes_core_timestamp_to_naive_utc(
    tmp_path: Path, modified_element: str, expected: datetime
) -> None:
    package = _write_package(tmp_path / "plan.xlsx", core_properties=_core_xml(modified_element))

    modified_at = _workbook_modified_at(package, _metadata())

    assert modified_at == expected


def test_workbook_modified_at_keeps_naive_core_timestamp(tmp_path: Path) -> None:
    package = _write_package(
        tmp_path / "plan.xlsx",
        core_properties=_core_xml("<dcterms:modified>2026-07-25T13:15:00</dcterms:modified>"),
    )

    modified_at = _workbook_modified_at(package, _metadata())

    assert modified_at == datetime.fromisoformat("2026-07-25T13:15")


def test_workbook_modified_at_falls_back_to_file_mtime_without_core_part(tmp_path: Path) -> None:
    package = _write_package(tmp_path / "plan.xlsx", core_properties=None)

    modified_at = _workbook_modified_at(package, _metadata())

    assert modified_at == datetime.fromisoformat("2026-01-01")
