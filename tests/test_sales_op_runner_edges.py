"""Pure decision edges of the sales_op runner — no database and no IMAP."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sales_op.models import ParsedSalesOpFile, SelectedSalesOpFile
from sales_op.runner import _choose_latest_files, _inspect_candidate, _parse_selected_files
from sales_op.specification import SPECIFICATION_FILE_NAME, read_sales_op_specification
from source_resolver import SourceFile
from tests.conftest import PROJECT_DIR, workbook_metadata

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from dictionary_models import DbRow

SPECIFICATION_PATH = PROJECT_DIR / "mock_data" / "sales_op" / SPECIFICATION_FILE_NAME
JULY = date(2026, 7, 1)


def _selected(
    path: Path,
    *,
    plan_date: date = JULY,
    modified_at: datetime = datetime(2026, 7, 1, tzinfo=UTC),
    source_code: str = "mfk",
    original_name: str | None = None,
) -> SelectedSalesOpFile:
    name = original_name or path.name
    return SelectedSalesOpFile(
        source_code=source_code,
        path=path,
        display_name=name,
        original_name=name,
        plan_date=plan_date,
        modified_at=modified_at,
        metadata=workbook_metadata(moment=modified_at),
        history_reference=str(path),
    )


class TestChooseLatestFiles:
    def test_duplicate_date_winner_prefers_newest_modified_at(self, tmp_path: Path) -> None:
        older = _selected(tmp_path / "old.xlsx", modified_at=datetime(2026, 7, 1, 8, tzinfo=UTC))
        newer = _selected(tmp_path / "new.xlsx", modified_at=datetime(2026, 7, 1, 12, tzinfo=UTC))

        chosen, warnings = _choose_latest_files({("mfk", JULY): [older, newer]})

        assert chosen == {("mfk", JULY): (newer,)}
        assert warnings == 1

    def test_tied_modified_at_is_broken_by_casefolded_name(self, tmp_path: Path) -> None:
        moment = datetime(2026, 7, 1, 12, tzinfo=UTC)
        lower = _selected(tmp_path / "a.xlsx", modified_at=moment, original_name="a.xlsx")
        upper = _selected(tmp_path / "b.xlsx", modified_at=moment, original_name="B.xlsx")

        chosen, warnings = _choose_latest_files({("mfk", JULY): [lower, upper]})

        assert chosen[("mfk", JULY)] == (upper,)
        assert warnings == 1

    def test_distinct_plan_dates_all_survive_and_are_sorted(self, tmp_path: Path) -> None:
        first = _selected(tmp_path / "first.xlsx", plan_date=date(2026, 7, 2))
        second = _selected(tmp_path / "second.xlsx", plan_date=date(2026, 7, 1))

        chosen, warnings = _choose_latest_files({("mfk", JULY): [first, second]})

        assert chosen[("mfk", JULY)] == (second, first)
        assert warnings == 0


class TestInspectCandidate:
    def test_unreadable_workbook_returns_none_and_logs_one_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def unreadable(_path: Path, *, timezone: object) -> tuple[date, datetime, object]:  # noqa: ARG001
            msg = "bad zip"
            raise OSError(msg)

        monkeypatch.setattr("sales_op.runner.inspect_sales_op_workbook", unreadable)
        source = SourceFile(path=tmp_path / "broken.xlsx", display_name="broken.xlsx")

        candidate = _inspect_candidate(
            source,
            source_code="mfk",
            original_name="broken.xlsx",
            month_start=JULY,
            timezone=UTC,
        )

        assert candidate is None
        assert any("не удалось определить дату файла" in record.getMessage() for record in caplog.records)

    def test_plan_date_outside_the_month_folder_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        moment = datetime(2026, 7, 1, tzinfo=UTC)

        def wrong_date(_path: Path, *, timezone: object) -> tuple[date, datetime, object]:  # noqa: ARG001
            return date(2026, 8, 1), moment, workbook_metadata(moment=moment)

        monkeypatch.setattr("sales_op.runner.inspect_sales_op_workbook", wrong_date)
        source = SourceFile(path=tmp_path / "august.xlsx", display_name="august.xlsx")

        candidate = _inspect_candidate(
            source,
            source_code="mfk",
            original_name="august.xlsx",
            month_start=JULY,
            timezone=UTC,
        )

        assert candidate is None
        assert any("содержит дату 2026-08-01" in record.getMessage() for record in caplog.records)


class TestParseSelectedFiles:
    def test_failed_group_is_skipped_without_touching_the_other_group(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        specification = read_sales_op_specification(SPECIFICATION_PATH)
        good = _selected(tmp_path / "good.xlsx")
        bad = _selected(tmp_path / "bad.xlsx", source_code="bsk_soda")
        rows: DbRow = {"doc_yearmonth": JULY, "client_name": "Client A"}

        def fake_parse(path: Path, *, source_specification: object) -> ParsedSalesOpFile:  # noqa: ARG001
            if path == bad.path:
                msg = "bad workbook"
                raise ValueError(msg)
            return ParsedSalesOpFile(plan_date=JULY, sheet_name="План", rows=[rows], warning_count=2)

        monkeypatch.setattr("sales_op.runner.parse_sales_op_workbook", fake_parse)

        parsed = _parse_selected_files(
            {("mfk", JULY): (good,), ("bsk_soda", JULY): (bad,)},
            failed_groups=frozenset({("bsk_soda", JULY)}),
            specification_sources=specification.sources,
        )

        assert set(parsed.rows_by_group) == {("mfk", JULY)}
        assert parsed.rows_by_group[("mfk", JULY)] == [rows]
        assert [entry.dest_table for entry in parsed.history_by_group[("mfk", JULY)]] == ["sales_op_mfk"]
        assert parsed.warnings == 2
        assert parsed.failures == 0  # the failed group was already counted during selection

    def test_parse_failure_discards_the_whole_group(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        specification = read_sales_op_specification(SPECIFICATION_PATH)
        good = _selected(tmp_path / "good.xlsx")
        bad = _selected(tmp_path / "bad.xlsx", original_name="bad2.xlsx")

        def fake_parse(path: Path, *, source_specification: object) -> ParsedSalesOpFile:  # noqa: ARG001
            if path == bad.path:
                msg = "bad workbook"
                raise ValueError(msg)
            return ParsedSalesOpFile(plan_date=JULY, sheet_name="План", rows=[], warning_count=0)

        monkeypatch.setattr("sales_op.runner.parse_sales_op_workbook", fake_parse)

        parsed = _parse_selected_files(
            {("mfk", JULY): (good, bad)},
            failed_groups=frozenset(),
            specification_sources=specification.sources,
        )

        assert parsed.rows_by_group == {}
        assert parsed.history_by_group == {}
        assert parsed.failures == 1
