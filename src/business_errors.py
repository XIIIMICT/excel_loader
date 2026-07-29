from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class BusinessLoadError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SourceFileNotFoundError(BusinessLoadError):
    def __init__(self, source_path: Path) -> None:
        super().__init__("файл не найден")
        self.source_path = source_path


class SourceColumnMissingError(BusinessLoadError):
    def __init__(self, column_name: str) -> None:
        super().__init__(f"отсутствует столбец {column_name!r}")
        self.column_name = column_name


class FileMetadataMissingError(BusinessLoadError):
    def __init__(self) -> None:
        super().__init__("не удалось прочитать обязательные метаданные файла")


class InvalidCellValueError(BusinessLoadError):
    def __init__(self, row_number: int, column_name: str, reason: str) -> None:
        super().__init__(f"{reason} в строке {row_number}, столбце {column_name!r}")
        self.row_number = row_number
        self.column_name = column_name


class CellTypeConversionError(BusinessLoadError):
    def __init__(self, cell_reference: str, expected_type: str, value: object) -> None:
        super().__init__(
            f"не удалось привести значение {value!r} в ячейке {cell_reference} к типу {expected_type}",
        )
        self.cell_reference = cell_reference
        self.expected_type = expected_type


class DuplicatePrimaryKeyError(BusinessLoadError):
    def __init__(self, primary_key_columns: tuple[str, ...], row_number: int | None = None) -> None:
        key_columns = " + ".join(primary_key_columns)
        if row_number is None:
            reason = f"первичный ключ {key_columns} не уникален"
        else:
            reason = f"первичный ключ {key_columns} не уникален, строка {row_number}"

        super().__init__(reason)
        self.primary_key_columns = primary_key_columns
        self.row_number = row_number


class RequiredCellValueError(BusinessLoadError):
    def __init__(self, cell_reference: str, column_name: str, value: object) -> None:
        super().__init__(
            f"обязательное поле {column_name!r} пусто в ячейке {cell_reference}, значение={value!r}",
        )


class DuplicateCellValueError(BusinessLoadError):
    def __init__(
        self,
        column_name: str,
        value: object,
        cell_reference: str,
        first_cell_reference: str,
    ) -> None:
        super().__init__(
            f"поле {column_name!r} должно быть уникальным: значение {value!r} повторяется "
            f"в ячейках {first_cell_reference} и {cell_reference}",
        )


class DatabaseOperationError(BusinessLoadError):
    @classmethod
    def from_dbapi_error(
        cls,
        error: Exception,
        *,
        operation: str,
        table_reference: str,
    ) -> DatabaseOperationError:
        original = getattr(error, "orig", error)
        diagnostic = getattr(original, "diag", None)
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        column_name = getattr(diagnostic, "column_name", None)
        constraint_name = getattr(diagnostic, "constraint_name", None)
        primary_message = getattr(diagnostic, "message_primary", None) or str(original).splitlines()[0]
        detail = getattr(diagnostic, "message_detail", None)
        context = [f"таблица={table_reference}"]
        if column_name:
            context.append(f"столбец={column_name}")
        if constraint_name:
            context.append(f"ограничение={constraint_name}")
        if sqlstate:
            context.append(f"SQLSTATE={sqlstate}")
        reason = f"{operation}: {', '.join(context)}; {primary_message}"
        if detail:
            reason += f"; {detail}"
        return cls(reason)
