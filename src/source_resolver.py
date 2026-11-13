import imaplib
import re
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from email.policy import default
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator
    from email.message import EmailMessage, Message

    from config import PatternOrPath, Settings, SourcesConfig


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: Path
    display_name: str
    original_name: str | None = None

    @property
    def history_reference(self) -> str:
        if self.original_name is not None and self.display_name.startswith("IMAP:"):
            return f"imap/{self.original_name}"
        return str(self.path)


class SourceError(Exception):
    pass


class SourceFileNotFoundError(SourceError):
    """
    A source file is missing on disk (or its IMAP attachment is absent).

    Distinct from a generic ``SourceError`` (a fatal infra problem such as an
    IMAP failure): a missing file is a WARNING + skip, not a load failure.
    Catch ``except SourceFileNotFoundError`` **before** ``except SourceError``
    so the skip path is type-driven, not matched on the Russian message text.
    """

    def __init__(self, source_path: Path) -> None:
        super().__init__(f"файл не найден: {source_path}")
        self.source_path = source_path


class SourceAuthError(SourceError):
    pass


@contextmanager
def resolve_source(
    settings: Settings,
    *,
    source_config: SourcesConfig,
    data_dir: Path,
    default_pattern_or_path: PatternOrPath,
) -> Generator[SourceFile]:
    if source_config.source == "local":
        yield _resolve_folder_source(data_dir=data_dir, pattern_or_path=default_pattern_or_path)
        return

    if source_config.source == "imap":
        with _resolve_imap_sources(
            settings,
            file_pattern=_exact_file_pattern(default_pattern_or_path),
            source_config=source_config,
        ) as source_files:
            if not source_files:
                msg = "в подходящем письме не найдено вложение с ожидаемым именем"
                raise SourceError(msg)
            yield source_files[0]
            return

    msg = f"неизвестный режим источника: {source_config.source!r}"
    raise SourceError(msg)


@contextmanager
def resolve_pattern_sources(
    settings: Settings,
    *,
    source_config: SourcesConfig,
    data_dir: Path,
    default_file_pattern: str,
) -> Generator[tuple[SourceFile, ...]]:
    file_pattern = source_config.file_pattern or default_file_pattern
    if source_config.source == "local":
        yield _resolve_folder_sources_by_pattern(data_dir=data_dir, file_pattern=file_pattern)
        return

    if source_config.source == "imap":
        with _resolve_imap_sources(
            settings,
            file_pattern=file_pattern,
            source_config=source_config,
        ) as source_files:
            yield source_files
            return

    msg = f"неизвестный режим источника: {source_config.source!r}"
    raise SourceError(msg)


@contextmanager
def resolve_all_pattern_sources(  # noqa: PLR0913
    settings: Settings,
    *,
    source_config: SourcesConfig,
    data_dir: Path,
    default_file_pattern: str,
    recursive_local: bool = False,
    skip_processed_sales_op_files: bool = False,
) -> Generator[tuple[SourceFile, ...]]:
    file_pattern = source_config.file_pattern or default_file_pattern
    if source_config.source == "local":
        yield _resolve_folder_sources_by_pattern(
            data_dir=data_dir,
            file_pattern=file_pattern,
            recursive=recursive_local,
            allow_empty=True,
            skip_processed_sales_op_files=skip_processed_sales_op_files,
        )
        return

    if source_config.source == "imap":
        with _resolve_imap_sources(
            settings,
            file_pattern=file_pattern,
            source_config=source_config,
            collect_all=True,
            skip_processed_sales_op_files=skip_processed_sales_op_files,
        ) as source_files:
            yield source_files
        return

    msg = f"неизвестный режим источника: {source_config.source!r}"
    raise SourceError(msg)


def _resolve_folder_source(*, data_dir: Path, pattern_or_path: PatternOrPath) -> SourceFile:
    source_path = Path(pattern_or_path)
    if not source_path.is_absolute():
        source_path = data_dir / source_path
    if not source_path.exists():
        raise SourceFileNotFoundError(source_path)
    if not source_path.is_file():
        msg = f"источник не является файлом: {source_path}"
        raise SourceError(msg)
    return SourceFile(path=source_path, display_name=str(source_path))


def _resolve_folder_sources_by_pattern(
    *,
    data_dir: Path,
    file_pattern: str,
    recursive: bool = False,
    allow_empty: bool = False,
    skip_processed_sales_op_files: bool = False,
) -> tuple[SourceFile, ...]:
    if not data_dir.exists():
        msg = f"папка источника не найдена: {data_dir}"
        raise SourceError(msg)

    pattern = _compile_file_pattern(file_pattern)
    candidates = data_dir.rglob("*") if recursive else data_dir.iterdir()
    source_files = tuple(
        SourceFile(path=path, display_name=str(path), original_name=path.name)
        for path in sorted(candidates, key=lambda item: str(item).casefold())
        if path.is_file()
        and pattern.fullmatch(path.name)
        and not _is_excel_owner_file(path.name)
        and not (skip_processed_sales_op_files and _is_processed_sales_op_file(path.name))
    )
    if not source_files and not allow_empty:
        msg = f"в папке {data_dir} не найдено файлов по шаблону {file_pattern!r}"
        raise SourceError(msg)
    return source_files


@contextmanager
def _resolve_imap_sources(
    settings: Settings,
    *,
    file_pattern: str,
    source_config: SourcesConfig,
    collect_all: bool = False,
    skip_processed_sales_op_files: bool = False,
) -> Generator[tuple[SourceFile, ...]]:
    host, user, password = _imap_credentials(settings)
    try:
        imap = imaplib.IMAP4_SSL(host, settings.imap.port)
    except imaplib.IMAP4.error as error:
        msg = f"не удалось подключиться к IMAP: {error}"
        raise SourceAuthError(msg) from error
    with tempfile.TemporaryDirectory(prefix="excel-loader-") as temp_dir:
        try:
            try:
                imap.login(user, password)
            except imaplib.IMAP4.error as error:
                msg = f"ошибка IMAP-аутентификации: {error}"
                raise SourceAuthError(msg) from error
            imap.select(settings.imap.mailbox)
            yield _download_matching_attachments(
                settings,
                imap,
                Path(temp_dir),
                file_pattern=file_pattern,
                source_config=source_config,
                collect_all=collect_all,
                skip_processed_sales_op_files=skip_processed_sales_op_files,
            )
        finally:
            with suppress(imaplib.IMAP4.error):
                imap.close()
            with suppress(imaplib.IMAP4.error):
                imap.logout()


def _imap_credentials(settings: Settings) -> tuple[str, str, str]:
    host = settings.imap.host
    user = settings.imap.user
    password = settings.imap.password
    if host is None or user is None or password is None:
        msg = "для IMAP-источника нужно заполнить common.imap.host, common.imap.user и common.imap.password"
        raise SourceError(msg)
    return host, user, password


def _download_matching_attachments(  # noqa: PLR0913
    settings: Settings,
    imap: imaplib.IMAP4,
    temp_dir: Path,
    *,
    file_pattern: str,
    source_config: SourcesConfig,
    collect_all: bool,
    skip_processed_sales_op_files: bool = False,
) -> tuple[SourceFile, ...]:
    status, payload = imap.search(None, "ALL")
    if status != "OK" or not payload or not payload[0]:
        msg = "во входящих не найдено писем"
        raise SourceError(msg)

    pattern = _compile_file_pattern(file_pattern)
    message_ids = payload[0].split()
    source_files: list[SourceFile] = []
    for message_id in reversed(message_ids[-settings.imap.scan_depth :]):
        decoded_message_id = message_id.decode("ascii")
        message = _fetch_email_message(imap, decoded_message_id)
        subject = _decode_header_text(message.get("Subject", ""))
        if not source_config.compiled_imap_subject_pattern.search(subject):
            continue

        attachments = _matching_attachments(
            message,
            pattern,
            skip_processed_sales_op_files=skip_processed_sales_op_files,
        )
        if not attachments:
            continue

        for file_name, content in attachments:
            target_path = temp_dir / f"{decoded_message_id}_{len(source_files)}_{file_name}"
            target_path.write_bytes(content)
            source_files.append(
                SourceFile(
                    path=target_path,
                    display_name=f"IMAP: {subject} / {file_name}",
                    original_name=file_name,
                ),
            )
        if source_files and not collect_all:
            return tuple(source_files)

    return tuple(source_files)


def _fetch_email_message(imap: imaplib.IMAP4, message_id: str) -> EmailMessage | Message:
    status, payload = imap.fetch(message_id, "(BODY.PEEK[])")
    if status != "OK" or not payload:
        msg = f"не удалось прочитать письмо IMAP id={message_id!r}"
        raise SourceError(msg)

    for item in payload:
        if isinstance(item, tuple):
            return message_from_bytes(item[1], policy=default)

    msg = f"не удалось прочитать тело письма IMAP id={message_id!r}"
    raise SourceError(msg)


def _matching_attachments(
    message: EmailMessage | Message,
    pattern: re.Pattern[str],
    *,
    skip_processed_sales_op_files: bool = False,
) -> tuple[tuple[str, bytes], ...]:
    result: list[tuple[str, bytes]] = []
    for part in message.walk():
        if part.get_content_disposition() != "attachment":
            continue

        file_name = part.get_filename()
        if file_name is None:
            continue

        decoded_file_name = _decode_header_text(file_name)
        if not pattern.fullmatch(decoded_file_name):
            continue
        if _is_excel_owner_file(decoded_file_name):
            continue
        if skip_processed_sales_op_files and _is_processed_sales_op_file(decoded_file_name):
            continue

        content = part.get_payload(decode=True)
        if isinstance(content, bytes):
            result.append((decoded_file_name, content))
    return tuple(sorted(result, key=lambda item: item[0]))


def _exact_file_pattern(pattern_or_path: PatternOrPath) -> str:
    return re.escape(Path(pattern_or_path).name)


def _is_excel_owner_file(file_name: str) -> bool:
    # Excel writes a ``~$<original>`` owner/lock file next to a workbook while
    # it is open. The file is a tiny non-OOXML stub, so reading it as xlsx
    # raises ``BadZipFile``; every enumeration path skips it regardless of the
    # configured file pattern so an open workbook never looks like a source.
    return file_name.startswith("~$")


def _is_processed_sales_op_file(file_name: str) -> bool:
    # A sales_op file successfully loaded in a previous run is renamed with a
    # leading underscore; future runs must not load the same plan again.
    return file_name.startswith("_")


def _compile_file_pattern(file_pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(file_pattern, re.IGNORECASE)
    except re.error as error:
        msg = f"file_pattern содержит некорректное регулярное выражение: {error}"
        raise SourceError(msg) from error


def _decode_header_text(value: str) -> str:
    decoded_parts: list[str] = []
    for content, encoding in decode_header(value):
        if isinstance(content, bytes):
            decoded_parts.append(content.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded_parts.append(content)

    return "".join(decoded_parts)
