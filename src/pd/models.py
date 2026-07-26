from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    xls_row: int | None

    def as_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "xls_row": self.xls_row,
        }


@dataclass(frozen=True, slots=True)
class RawMaterial:
    asset_name: str
    ordinal_number: int
    name: str
    parent_name: str | None
    material_path: tuple[str, ...]
    xls_row: int | None

    def as_record(self) -> dict[str, Any]:
        return {
            "asset_name": self.asset_name,
            "ordinal_number": self.ordinal_number,
            "name": self.name,
            "parent_name": self.parent_name,
            "material_path": self.material_path,
            "xls_row": self.xls_row,
        }


@dataclass(frozen=True, slots=True)
class Indicator:
    xls_col: str | None
    xls_col_index: int | None
    ordinal_number: int
    category_1: str | None
    category_2: str | None
    category_3: str | None
    name: str
    db_name: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "xls_col": self.xls_col,
            "xls_col_index": self.xls_col_index,
            "ordinal_number": self.ordinal_number,
            "name": self.name,
            "category_1": self.category_1,
            "category_2": self.category_2,
            "category_3": self.category_3,
            "db_name": self.db_name,
        }

    @property
    def full_name(self) -> str:
        return " | ".join(part for part in (self.category_1, self.category_2, self.category_3, self.name) if part)


@dataclass(frozen=True, slots=True)
class ParsedReport:
    source_name: str
    report_date: date
    loaded_at: datetime
    assets: tuple[Asset, ...]
    raw_materials: tuple[RawMaterial, ...]
    indicators: tuple[Indicator, ...]
    data: pd.DataFrame

    @property
    def assets_frame(self) -> pd.DataFrame:
        frame = pd.DataFrame([asset.as_record() for asset in self.assets])
        frame["loaded_at"] = self.loaded_at
        return _with_nullable_ints(frame, ["xls_row"])

    @property
    def raw_materials_frame(self) -> pd.DataFrame:
        frame = pd.DataFrame([raw_material.as_record() for raw_material in self.raw_materials])
        frame["loaded_at"] = self.loaded_at
        return _with_nullable_ints(frame, ["ordinal_number", "xls_row"])

    @property
    def indicators_frame(self) -> pd.DataFrame:
        frame = pd.DataFrame([indicator.as_record() for indicator in self.indicators])
        frame["loaded_at"] = self.loaded_at
        return _with_nullable_ints(frame, ["xls_col_index"])


def _with_nullable_ints(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for column in columns:
        if column in frame.columns:
            frame[column] = frame[column].astype("Int64")
    return frame


DbScalar = str | int | Decimal | date | datetime | None
