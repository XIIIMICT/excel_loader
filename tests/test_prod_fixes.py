"""
Production-driven regressions for three prod complaints.

* sales_op opens workbooks ``read_only=True``; openpyxl parses the sheet
  lazily, so the ``"Unknown extension … will be removed"`` warning fires
  during ``iter_rows``, after ``load_openpyxl_workbook`` has returned.
  ``_workbook_warnings_suppressed`` wraps the whole workbook lifecycle and
  narrows on message+category+module so genuine conversion warnings survive.
* Excel writes ``~$<original>`` owner files next to an open workbook; reading
  one as xlsx is a ``BadZipFile``, so every enumeration path skips them.
* ``assign_object_owner.sql`` no longer hardcodes a role name: the loader
  injects ``common.postgres.user`` (``POSTGRES_USER``) into the ``DO`` body as
  an escaped string literal.
"""

import logging
import re
import warnings
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from openpyxl import Workbook

from excel_parser import load_openpyxl_workbook
from main import SOURCE_OWNER_PLACEHOLDER, _render_post_action_sql, _sql_string_literal
from sales_op.excel_parser import _workbook_warnings_suppressed
from source_resolver import _is_excel_owner_file, _resolve_folder_sources_by_pattern

if TYPE_CHECKING:
    import pytest

    from config import Settings

UNKNOWN_EXTENSION_URI = "{00000000-0000-0000-0000-000000000000}"
EXTENSION_WARNING_RE = re.compile(r"extension is not supported and will be removed")


def _xlsx_with_unknown_extension(path: Path) -> None:
    """
    Save a one-cell workbook, then inject an ``extLst`` openpyxl cannot map.

    ``parse_extensions`` (openpyxl/worksheet/_reader.py) warns ``"<type>
    extension is not supported and will be removed"`` for every ``<ext>`` whose
    ``uri`` is absent from ``EXT_TYPES``; the GUID below is not a known one.
    """
    workbook = Workbook()
    worksheet = workbook.active
    worksheet["A1"] = "x"
    workbook.save(path)

    rewritten = path.with_suffix(".patched.xlsx")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(rewritten, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                text = data.decode("utf-8")
                extension_list = f'<extLst><ext uri="{UNKNOWN_EXTENSION_URI}"/></extLst>'
                data = text.replace("</worksheet>", extension_list + "</worksheet>").encode("utf-8")
            target.writestr(item, data)
    rewritten.replace(path)


def test_workbook_warnings_suppressed_hides_lazy_openpyxl_extension_warning(tmp_path: Path) -> None:
    """The warning fires during ``iter_rows`` (read_only), not ``load_workbook``."""
    source_path = tmp_path / "with_extension.xlsx"
    _xlsx_with_unknown_extension(source_path)

    # Sanity: without the sales_op scope, the lazy-read warning leaks.
    with warnings.catch_warnings(record=True) as unsuppressed:
        warnings.simplefilter("always")
        workbook = load_openpyxl_workbook(source_path, data_only=True, read_only=True)
        tuple(workbook.active.iter_rows(values_only=True))
        workbook.close()
    leaked = [w for w in unsuppressed if EXTENSION_WARNING_RE.search(str(w.message))]
    assert leaked, "fixture must trigger the openpyxl extension warning on read"

    # Under the sales_op scope the same read stays quiet.
    with warnings.catch_warnings(record=True) as suppressed:
        warnings.simplefilter("always")
        with _workbook_warnings_suppressed(source_path):
            workbook = load_openpyxl_workbook(source_path, data_only=True, read_only=True)
            tuple(workbook.active.iter_rows(values_only=True))
            workbook.close()
    assert not [w for w in suppressed if EXTENSION_WARNING_RE.search(str(w.message))]


def test_workbook_warnings_suppressed_does_not_mask_unrelated_user_warning(tmp_path: Path) -> None:
    """The filter is narrow: an unrelated UserWarning still surfaces."""
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with _workbook_warnings_suppressed(tmp_path / "book.xlsx"):
            warnings.warn("unrelated conversion problem", UserWarning, stacklevel=2)
    assert any("unrelated conversion problem" in str(w.message) for w in captured)


def test_workbook_warnings_suppressed_logs_openpyxl_date_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Openpyxl's date-serial warning is hidden, but logged with the file name."""
    source_path = tmp_path / "bad_date.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet["A1"] = 99999999  # serial beyond the max Excel date
    worksheet["A1"].number_format = "dd.mm.yyyy"
    worksheet["B1"] = "ok"
    workbook.save(source_path)

    with (
        warnings.catch_warnings(record=True) as leaked,
        caplog.at_level(logging.WARNING, logger="excel_loader"),
        _workbook_warnings_suppressed(source_path),
    ):
        warnings.simplefilter("always")
        book = load_openpyxl_workbook(source_path, data_only=True, read_only=True)
        values = tuple(book.active.iter_rows(values_only=True))
        book.close()

    assert not leaked, "openpyxl's date warning must not reach the default warning handler"
    assert values == (("#VALUE!", "ok"),)
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "openpyxl" in message and str(source_path) in message and "A1" in message and "99999999" in message
        for message in messages
    ), messages


def test_is_excel_owner_file_detects_lock_prefix() -> None:
    assert _is_excel_owner_file("~$report.xlsx")
    assert _is_excel_owner_file("~$~normal.xlsx")
    assert not _is_excel_owner_file("report.xlsx")
    assert not _is_excel_owner_file("backup~$.xlsx")  # prefix, not suffix
    assert not _is_excel_owner_file("")


def test_resolve_folder_sources_skips_excel_owner_lock_files(tmp_path: Path) -> None:
    (tmp_path / "report.xlsx").write_bytes(b"")
    (tmp_path / "~$report.xlsx").write_bytes(b"")  # owner file while report is open
    (tmp_path / "notes.txt").write_bytes(b"")

    sources = _resolve_folder_sources_by_pattern(
        data_dir=tmp_path,
        file_pattern=r".*\.xlsx$",
        allow_empty=True,
    )

    assert [source.path.name for source in sources] == ["report.xlsx"]


def test_render_post_action_sql_substitutes_source_owner_placeholder(settings: Settings) -> None:
    postgres = settings.common.postgres.model_copy(update={"user": "loader_role"})
    common = settings.common.model_copy(update={"postgres": postgres})
    settings = settings.model_copy(update={"common": common})

    rendered = _render_post_action_sql(
        "AND owner_role.rolname = __SOURCE_OWNER__",
        settings=settings,
    )

    assert "__SOURCE_OWNER__" not in rendered
    assert rendered == "AND owner_role.rolname = 'loader_role'"


def test_render_post_action_sql_leaves_placeholder_free_sql_untouched(settings: Settings) -> None:
    assert _render_post_action_sql("SELECT 1;", settings=settings) == "SELECT 1;"


def test_sql_string_literal_escapes_single_quotes() -> None:
    # Role names cannot legitimately contain quotes, but the renderer must not
    # become an SQL-injection vector if one ever does.
    assert _sql_string_literal("o'brien") == "'o''brien'"
    assert _sql_string_literal("plain") == "'plain'"


def test_assign_object_owner_sql_uses_token_not_hardcoded_user() -> None:
    project_dir = Path(__file__).resolve().parents[1]
    sql = (project_dir / "sql" / "post_actions" / "assign_object_owner.sql").read_text(encoding="utf-8")

    assert "evtushenko" not in sql
    assert SOURCE_OWNER_PLACEHOLDER in sql
