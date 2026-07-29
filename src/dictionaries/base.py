from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from config import Settings
from dictionary_models import CombinedDictionaryPayload, DictionaryPayload

type BuildPayloadFn = Callable[[Settings, Path], DictionaryPayload]
type BuildCombinedPayloadFn = Callable[[Settings, tuple[Path, ...]], CombinedDictionaryPayload]
type NormalizationStyle = Literal["capitalize", "uppercase"]


@dataclass(frozen=True, slots=True)
class DictionaryLoader:
    table_name: str
    source_path: str
    build_payload: BuildPayloadFn
    columns_to_normalize: tuple[str, ...] = ()
    normalization_styles: dict[str, NormalizationStyle] | None = None

    def normalization_style_for(self, column_name: str) -> NormalizationStyle:
        if self.normalization_styles is None:
            return "capitalize"
        return self.normalization_styles.get(column_name, "capitalize")


@dataclass(frozen=True, slots=True)
class CombinedDictionaryLoader:
    table_name: str
    source_paths: tuple[str, ...]
    build_payload: BuildCombinedPayloadFn
