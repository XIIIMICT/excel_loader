import json
import re
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TypedDict, cast

import pandas as pd

from pd.models import Asset, Indicator, ParsedReport, RawMaterial

MANIFEST_FILE_NAME = "manifest.json"
ASSETS_FILE_NAME = "assets.json"
RAW_MATERIALS_FILE_NAME = "raw_materials.json"
INDICATORS_FILE_NAME = "indicators.json"
DATA_FILE_NAME = "data.json"

type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
type JsonObject = dict[str, JsonValue]


class AssetRecord(TypedDict):
    name: str
    xls_row: int | None


class RawMaterialRecord(TypedDict):
    asset_name: str
    ordinal_number: int
    name: str
    parent_name: str | None
    material_path: tuple[str, ...]
    xls_row: int | None


class IndicatorRecord(TypedDict):
    xls_col: str | None
    xls_col_index: int | None
    ordinal_number: int
    category_1: str | None
    category_2: str | None
    category_3: str | None
    name: str
    db_name: str | None


def save_unloaded_report(unloaded_data_dir: Path, report: ParsedReport) -> Path:
    unloaded_data_dir.mkdir(parents=True, exist_ok=True)
    package_dir = unloaded_data_dir / _package_name(report)
    package_dir.mkdir(parents=True, exist_ok=False)

    _write_json(
        package_dir / MANIFEST_FILE_NAME,
        {
            "source_name": report.source_name,
            "report_date": report.report_date,
            "loaded_at": report.loaded_at,
        },
    )
    _write_json(package_dir / ASSETS_FILE_NAME, [asdict(asset) for asset in report.assets])
    _write_json(package_dir / RAW_MATERIALS_FILE_NAME, [asdict(raw_material) for raw_material in report.raw_materials])
    _write_json(package_dir / INDICATORS_FILE_NAME, [asdict(indicator) for indicator in report.indicators])
    _write_json(package_dir / DATA_FILE_NAME, report.data.to_dict(orient="records"))
    return package_dir


def load_unloaded_reports(unloaded_data_dir: Path) -> list[tuple[Path, ParsedReport]]:
    if not unloaded_data_dir.exists():
        return []

    package_dirs = sorted(path for path in unloaded_data_dir.iterdir() if path.is_dir())
    return [(package_dir, _load_unloaded_report(package_dir)) for package_dir in package_dirs]


def delete_unloaded_report(package_dir: Path) -> None:
    for child in package_dir.iterdir():
        child.unlink()
    package_dir.rmdir()


def _load_unloaded_report(package_dir: Path) -> ParsedReport:
    manifest = cast("JsonObject", _read_json(package_dir / MANIFEST_FILE_NAME))
    loaded_at = datetime.fromisoformat(str(manifest["loaded_at"]))
    assets = _load_asset_records(package_dir / ASSETS_FILE_NAME)
    raw_materials = _load_raw_material_records(package_dir / RAW_MATERIALS_FILE_NAME)
    indicators = _load_indicator_records(package_dir / INDICATORS_FILE_NAME)
    data_records = cast("list[JsonObject]", _read_json(package_dir / DATA_FILE_NAME))
    data = pd.DataFrame(_decode_records(data_records))
    return ParsedReport(
        source_name=str(manifest["source_name"]),
        report_date=date.fromisoformat(str(manifest["report_date"])),
        loaded_at=loaded_at,
        assets=tuple(Asset(**record) for record in assets),
        raw_materials=tuple(RawMaterial(**record) for record in raw_materials),
        indicators=tuple(Indicator(**record) for record in indicators),
        data=data,
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(_encode_value(value), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_json(path: Path) -> object:
    raw_value = cast("JsonValue", json.loads(path.read_text(encoding="utf-8")))
    return _decode_value(raw_value)


def _encode_value(value: object) -> JsonValue:
    encoded: JsonValue
    if isinstance(value, dict):
        encoded = {str(key): _encode_value(item) for key, item in value.items()}
    elif isinstance(value, list | tuple):
        encoded = [_encode_value(item) for item in value]
    elif isinstance(value, Decimal):
        encoded = {"__type__": "decimal", "value": str(value)}
    elif isinstance(value, datetime):
        encoded = {"__type__": "datetime", "value": value.isoformat()}
    elif isinstance(value, date):
        encoded = {"__type__": "date", "value": value.isoformat()}
    elif pd.isna(value):
        encoded = None
    else:
        encoded = cast("JsonPrimitive", value)
    return encoded


def _decode_records(records: list[JsonObject]) -> list[dict[str, object]]:
    return [{key: _decode_value(value) for key, value in record.items()} for record in records]


def _decode_value(value: JsonValue) -> object:
    decoded: object
    if isinstance(value, dict) and value.get("__type__") == "decimal":
        decoded = Decimal(str(value["value"]))
    elif isinstance(value, dict) and value.get("__type__") == "datetime":
        decoded = datetime.fromisoformat(str(value["value"]))
    elif isinstance(value, dict) and value.get("__type__") == "date":
        decoded = date.fromisoformat(str(value["value"]))
    elif isinstance(value, dict):
        decoded = {key: _decode_value(item) for key, item in value.items()}
    elif isinstance(value, list):
        decoded = [_decode_value(item) for item in value]
    else:
        decoded = value
    return decoded


def _package_name(report: ParsedReport) -> str:
    timestamp = report.loaded_at.strftime("%Y%m%d_%H%M%S_%f")
    source_stem = re.sub(r"[^A-Za-z0-9А-Яа-я_-]+", "_", Path(report.source_name).stem).strip("_")
    return f"{timestamp}_{source_stem or 'pd_report'}"


def _load_asset_records(path: Path) -> list[AssetRecord]:
    return cast("list[AssetRecord]", _read_json(path))


def _load_raw_material_records(path: Path) -> list[RawMaterialRecord]:
    records = cast("list[dict[str, object]]", _read_json(path))
    return [
        cast(
            "RawMaterialRecord",
            {
                **record,
                "material_path": tuple(cast("list[str]", record["material_path"])),
            },
        )
        for record in records
    ]


def _load_indicator_records(path: Path) -> list[IndicatorRecord]:
    return cast("list[IndicatorRecord]", _read_json(path))
