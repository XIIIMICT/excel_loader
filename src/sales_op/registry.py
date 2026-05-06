import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SalesOpSourceDefinition:
    code: str
    table_name: str
    file_pattern: str
    required_name_parts: tuple[str, ...]

    def matches_file_name(self, file_name: str) -> bool:
        return re.fullmatch(self.file_pattern, file_name, flags=re.IGNORECASE) is not None

    def matches_heading(self, heading: object) -> bool:
        if not isinstance(heading, str):
            return False
        normalized_heading = heading.casefold()
        return all(part.casefold() in normalized_heading for part in self.required_name_parts)


SOURCE_DEFINITIONS = (
    SalesOpSourceDefinition(
        code="mfk",
        table_name="sales_op_mfk",
        file_pattern=r"^(?=.*мфк).*\.xlsx$",
        required_name_parts=("МФК",),
    ),
    SalesOpSourceDefinition(
        code="bsk_hshp",
        table_name="sales_op_bsk_hshp",
        file_pattern=r"^(?=.*бск)(?=.*хщп).*\.xlsx$",
        required_name_parts=("БСК", "ХЩП"),
    ),
    SalesOpSourceDefinition(
        code="bsk_polymers",
        table_name="sales_op_bsk_polymers",
        file_pattern=r"^(?=.*бск)(?=.*полимеры).*\.xlsx$",
        required_name_parts=("БСК", "Полимеры"),
    ),
    SalesOpSourceDefinition(
        code="bsk_mth",
        table_name="sales_op_bsk_mth",
        file_pattern=r"^(?=.*бск)(?=.*мтх).*\.xlsx$",
        required_name_parts=("БСК", "МТХ"),
    ),
    SalesOpSourceDefinition(
        code="bsk_obn",
        table_name="sales_op_bsk_obn",
        file_pattern=r"^(?=.*бск)(?=.*обн).*\.xlsx$",
        required_name_parts=("БСК", "ОБН"),
    ),
    SalesOpSourceDefinition(
        code="bsk_soda",
        table_name="sales_op_bsk_soda",
        file_pattern=r"^(?=.*бск)(?=.*сода).*\.xlsx$",
        required_name_parts=("БСК", "сода"),
    ),
)

SOURCE_DEFINITIONS_BY_CODE = {definition.code: definition for definition in SOURCE_DEFINITIONS}
