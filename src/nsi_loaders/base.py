from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from config import Settings
from dictionary_models import DictionaryPayload

type BuildPayloadFn = Callable[[Settings, Path], DictionaryPayload]
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
