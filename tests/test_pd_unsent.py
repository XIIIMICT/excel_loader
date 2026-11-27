"""Deferred PD packages must round-trip typed values, not stringify them."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pandas as pd
import pytest

from pd.models import Asset, Indicator, ParsedReport, RawMaterial

if TYPE_CHECKING:
    from pathlib import Path
from pd.unsent import (
    ASSETS_FILE_NAME,
    DATA_FILE_NAME,
    INDICATORS_FILE_NAME,
    MANIFEST_FILE_NAME,
    RAW_MATERIALS_FILE_NAME,
    JsonValue,
    _decode_value,
    _encode_value,
    _optional_datetime,
    _package_name,
    delete_unloaded_report,
    load_unloaded_reports,
    save_unloaded_report,
)

LOADED_AT = datetime(2026, 6, 24, 10, 15, 30, tzinfo=UTC)
MODIFIED_AT = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)


def _report(source_name: str = "pd.xlsx", *, loaded_at: datetime = LOADED_AT) -> ParsedReport:
    return ParsedReport(
        source_name=source_name,
        report_date=date(2026, 6, 23),
        loaded_at=loaded_at,
        assets=(Asset(name="БСК Сода", xls_row=10),),
        raw_materials=(
            RawMaterial(
                asset_name="БСК Сода",
                ordinal_number=1,
                name="Сырьё A",
                parent_name=None,
                material_path=("Сырьё A",),
                xls_row=11,
            ),
        ),
        indicators=(
            Indicator(
                xls_col="F",
                xls_col_index=6,
                ordinal_number=1,
                category_1=None,
                category_2=None,
                category_3=None,
                name="Выработка",
            ),
        ),
        data=pd.DataFrame(
            [
                {
                    "asset_name": "БСК Сода",
                    "raw_material_name": "Сырьё A",
                    "material_path": ("Сырьё A",),
                    "ordinal_number": 1,
                    "indicator_name": "Выработка",
                    "value": "1234.5",
                    "loaded_at": loaded_at,
                },
            ],
            dtype=object,
        ),
        history_reference="/data/pd.xlsx",
        file_modified_at=MODIFIED_AT,
        file_modifier="tester",
    )


class TestDeferredPackageRoundTrip:
    def test_saves_loads_and_deletes_a_package(self, tmp_path: Path) -> None:
        package_dir = save_unloaded_report(tmp_path, _report())

        assert package_dir.parent == tmp_path
        assert {path.name for path in package_dir.iterdir()} == {
            MANIFEST_FILE_NAME,
            ASSETS_FILE_NAME,
            RAW_MATERIALS_FILE_NAME,
            INDICATORS_FILE_NAME,
            DATA_FILE_NAME,
        }
        loaded = load_unloaded_reports(tmp_path)

        assert len(loaded) == 1
        assert loaded[0][0] == package_dir
        restored = loaded[0][1]
        assert restored.source_name == "pd.xlsx"
        assert restored.report_date == date(2026, 6, 23)
        assert restored.loaded_at == LOADED_AT
        assert restored.history_reference == "/data/pd.xlsx"
        assert restored.file_modified_at == MODIFIED_AT
        assert restored.file_modifier == "tester"
        assert restored.assets == _report().assets
        assert restored.raw_materials == _report().raw_materials
        assert restored.indicators == _report().indicators
        assert restored.data.iloc[0]["value"] == "1234.5"
        assert restored.data.iloc[0]["material_path"] == ["Сырьё A"]

        delete_unloaded_report(package_dir)
        assert load_unloaded_reports(tmp_path) == []

    def test_missing_directory_has_no_packages(self, tmp_path: Path) -> None:
        assert load_unloaded_reports(tmp_path / "absent") == []

    def test_packages_are_sorted_by_name(self, tmp_path: Path) -> None:
        save_unloaded_report(tmp_path, _report(loaded_at=datetime(2026, 6, 24, 11, tzinfo=UTC)))
        save_unloaded_report(tmp_path, _report(loaded_at=datetime(2026, 6, 24, 9, tzinfo=UTC)))

        packages = load_unloaded_reports(tmp_path)

        assert [report.loaded_at for _path, report in packages] == [
            datetime(2026, 6, 24, 9, tzinfo=UTC),
            datetime(2026, 6, 24, 11, tzinfo=UTC),
        ]

    def test_manifest_stores_typed_markers(self, tmp_path: Path) -> None:
        package_dir = save_unloaded_report(tmp_path, _report())

        manifest = (package_dir / MANIFEST_FILE_NAME).read_text(encoding="utf-8")
        assert '"__type__": "date"' in manifest
        assert '"__type__": "datetime"' in manifest
        assert "2026-06-23" in manifest
        assert "2026-06-24T10:15:30+00:00" in manifest


class TestTypedJsonValues:
    @pytest.mark.parametrize(
        ("value", "encoded"),
        [
            (Decimal("1.25"), {"__type__": "decimal", "value": "1.25"}),
            (date(2026, 6, 23), {"__type__": "date", "value": "2026-06-23"}),
            (LOADED_AT, {"__type__": "datetime", "value": "2026-06-24T10:15:30+00:00"}),
            (None, None),
            (float("nan"), None),
            ("plain", "plain"),
        ],
    )
    def test_encoding(self, value: object, encoded: object) -> None:
        assert _encode_value(value) == encoded

    def test_nested_values_round_trip_with_their_types(self) -> None:
        value = {
            "amount": Decimal("7.30"),
            "days": [date(2026, 6, 23), date(2026, 6, 24)],
            "nested": {"moment": LOADED_AT, "empty": None, "count": 2},
        }

        decoded = cast("dict[str, object]", _decode_value(_encode_value(value)))

        assert decoded["amount"] == Decimal("7.30")
        assert isinstance(decoded["amount"], Decimal)
        assert decoded["days"] == [date(2026, 6, 23), date(2026, 6, 24)]
        days = cast("list[date]", decoded["days"])
        assert all(isinstance(day, date) and not isinstance(day, datetime) for day in days)
        assert decoded["nested"] == {"moment": LOADED_AT, "empty": None, "count": 2}
        nested = cast("dict[str, object]", decoded["nested"])
        assert isinstance(nested["moment"], datetime)

    def test_data_frame_values_keep_their_types_after_a_round_trip(self, tmp_path: Path) -> None:
        report = _report()
        report.data.loc[0, "value"] = Decimal("12.50")
        report.data["extra_date"] = [date(2026, 6, 23)]
        report.data["extra_datetime"] = [LOADED_AT]
        package_dir = save_unloaded_report(tmp_path, report)

        restored = load_unloaded_reports(tmp_path)[0][1]
        row = restored.data.iloc[0]

        assert row["value"] == Decimal("12.50")
        assert isinstance(row["value"], Decimal)
        assert row["extra_date"] == date(2026, 6, 23)
        assert isinstance(row["extra_date"], date)
        assert row["extra_datetime"] == LOADED_AT
        assert isinstance(row["extra_datetime"], datetime)
        assert (package_dir / DATA_FILE_NAME).exists()


def test_optional_datetime_accepts_objects_strings_and_none() -> None:
    assert _optional_datetime(None) is None
    assert _optional_datetime(cast("JsonValue", LOADED_AT)) is LOADED_AT
    assert _optional_datetime("2026-06-24T10:15:30+00:00") == LOADED_AT


def test_package_name_sanitizes_the_source_stem() -> None:
    package_name = _package_name(_report(source_name="wéird/pd? report.xlsx"))

    assert package_name.endswith("_pd_report")
    assert package_name.startswith("20260624_101530_")
    assert "?" not in package_name
    assert "/" not in package_name
