import re
import tomllib
from collections.abc import Mapping, Sequence
from datetime import datetime, tzinfo
from os import environ
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.engine import URL

SRC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SRC_DIR.parent
CONFIG_PATH = PROJECT_DIR / "config.toml"
MAX_QUALIFIED_NAME_PARTS = 2
QUOTES_NSI_SMTP_ERROR_SUBJECT = "НСИ котировок: предупреждения и ошибки загрузки"
QUOTES_SMTP_ERROR_SUBJECT = "Котировки: предупреждения и ошибки загрузки"
PD_SMTP_ERROR_SUBJECT = "ПД: ошибка загрузки производственной сводки"
SALES_OP_SMTP_ERROR_SUBJECT = "План продаж: предупреждения и ошибки загрузки"


type PatternOrPath = str | Path
type SourceMode = str
type PostActionMode = Literal["success", "partial_success", "always"]


class SourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: Path
    source: SourceMode = "local"
    file_pattern: str | None = Field(default=None, min_length=1)
    imap_subject_pattern: str | None = Field(default=None, min_length=1)

    @field_validator("data_dir", mode="after")
    @classmethod
    def resolve_project_path(cls, value: Path) -> Path:
        if value.is_absolute():
            return value
        return PROJECT_DIR / value

    @field_validator("source", mode="after")
    @classmethod
    def validate_source(cls, value: SourceMode) -> SourceMode:
        normalized = value.strip().lower()
        if normalized not in {"local", "imap"}:
            msg = "source должен быть 'local' или 'imap'."
            raise ValueError(msg)
        return normalized

    @field_validator("file_pattern", mode="after")
    @classmethod
    def validate_file_pattern(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _compile_regex(value, setting_name="file_pattern")
        return value

    @field_validator("imap_subject_pattern", mode="after")
    @classmethod
    def validate_imap_subject_pattern(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _compile_regex(value, setting_name="imap_subject_pattern")
        return value

    @property
    def compiled_imap_subject_pattern(self) -> re.Pattern[str]:
        if self.imap_subject_pattern is None:
            msg = "imap_subject_pattern не настроен."
            raise ValueError(msg)
        return _compile_regex(self.imap_subject_pattern, setting_name="imap_subject_pattern")

    @property
    def compiled_file_pattern(self) -> re.Pattern[str]:
        if self.file_pattern is None:
            msg = "file_pattern is not configured."
            raise ValueError(msg)
        return _compile_regex(self.file_pattern, setting_name="file_pattern")


class PostgresConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="localhost", min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    user: str = Field(default="postgres", min_length=1)
    password: str = Field(default="strong_password", min_length=1)
    db: str = Field(default="dwh", min_length=1)
    schema_name: str = Field(default="raw", alias="schema", min_length=1)


class ImapConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str | None = None
    port: int = Field(default=993, ge=1, le=65535)
    user: str | None = None
    password: str | None = None
    mailbox: str = Field(default="INBOX", min_length=1)
    scan_depth: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def normalize_optional_text(self) -> Self:
        self.host = _strip_optional(self.host)
        self.user = _strip_optional(self.user)
        self.password = _strip_optional(self.password)
        return self


class SmtpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    user: str | None = None
    password: str | None = None
    from_address: str | None = None

    @model_validator(mode="after")
    def normalize_optional_text(self) -> Self:
        self.host = _strip_optional(self.host)
        self.user = _strip_optional(self.user)
        self.password = _strip_optional(self.password)
        self.from_address = _strip_optional(self.from_address)
        return self


class PostActionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    run_on: tuple[PostActionMode, ...]

    @field_validator("path", mode="after")
    @classmethod
    def resolve_project_path(cls, value: Path) -> Path:
        if value.is_absolute():
            return value
        return PROJECT_DIR / value

    @field_validator("run_on", mode="after")
    @classmethod
    def validate_run_on(cls, value: tuple[PostActionMode, ...]) -> tuple[PostActionMode, ...]:
        if not value:
            msg = "Для общего пост-действия требуется указать хотя бы одно состояние run_on."
            raise ValueError(msg)
        return tuple(dict.fromkeys(value))


class CommonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_dir: Path = Path("logs")
    unloaded_data_dir: Path = Path("unloaded_data")
    mock_data_dir: Path = Path("mock_data")
    timezone: str | None = None
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    imap: ImapConfig = Field(default_factory=ImapConfig)
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)
    post_actions: tuple[PostActionConfig, ...] = ()

    @field_validator("log_dir", "unloaded_data_dir", "mock_data_dir", mode="after")
    @classmethod
    def resolve_project_path(cls, value: Path) -> Path:
        if value.is_absolute():
            return value
        return PROJECT_DIR / value

    @field_validator("timezone", mode="after")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            msg = f"Неизвестная временная зона: {value!r}."
            raise ValueError(msg) from error
        return value


class ModuleBaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_name: str = Field(alias="schema", min_length=1)
    smtp_error_handling_on: bool = False
    smtp_recipients: tuple[str, ...] = ()
    mviews_to_refresh: tuple[str, ...] = ()
    users_to_grant: tuple[str, ...] = ()

    @field_validator("mviews_to_refresh", mode="after")
    @classmethod
    def validate_mviews(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_qualified_names(value, setting_name="mviews_to_refresh")


class ModuleConfig(ModuleBaseConfig):
    sources: SourcesConfig


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    common: CommonConfig = Field(default_factory=CommonConfig)
    quotes_nsi: ModuleConfig
    quotes: ModuleConfig
    pd: ModuleConfig
    sales_op: ModuleConfig

    @model_validator(mode="after")
    def validate_external_services(self) -> Self:
        _validate_imap_settings(self)
        _validate_smtp_settings(self)
        return self

    @property
    def app_timezone(self) -> tzinfo:
        if self.common.timezone is None:
            system_timezone = datetime.now().astimezone().tzinfo
            if system_timezone is not None:
                return system_timezone
            return ZoneInfo("UTC")
        return ZoneInfo(self.common.timezone)

    @property
    def logs_dir(self) -> Path:
        return self.common.log_dir

    @property
    def unloaded_data_dir(self) -> Path:
        return self.common.unloaded_data_dir

    @property
    def mock_data_dir(self) -> Path:
        return self.common.mock_data_dir

    @property
    def postgres(self) -> PostgresConfig:
        return self.common.postgres

    @property
    def imap(self) -> ImapConfig:
        return self.common.imap

    @property
    def smtp(self) -> SmtpConfig:
        return self.common.smtp

    @property
    def postgres_host(self) -> str:
        return self.postgres.host

    @property
    def postgres_port(self) -> int:
        return self.postgres.port

    @property
    def postgres_user(self) -> str:
        return self.postgres.user

    @property
    def postgres_password(self) -> str:
        return self.postgres.password

    @property
    def postgres_database(self) -> str:
        return self.postgres.db

    @property
    def table_schema(self) -> str:
        return self.postgres.schema_name

    @property
    def quotes_nsi_schema(self) -> str:
        return self.quotes_nsi.schema_name

    @property
    def quotes_schema(self) -> str:
        return self.quotes.schema_name

    @property
    def pd_schema(self) -> str:
        return self.pd.schema_name

    @property
    def sales_op_schema(self) -> str:
        return self.sales_op.schema_name

    @property
    def smtp_host(self) -> str | None:
        return self.smtp.host

    @property
    def smtp_port(self) -> int | None:
        return self.smtp.port

    @property
    def smtp_username(self) -> str | None:
        return self.smtp.user

    @property
    def smtp_password_secret(self) -> str | None:
        return self.smtp.password

    @property
    def smtp_from_address(self) -> str | None:
        return self.smtp.from_address

    @property
    def quotes_nsi_smtp_error_subject(self) -> str:
        return QUOTES_NSI_SMTP_ERROR_SUBJECT

    @property
    def quotes_smtp_error_subject(self) -> str:
        return QUOTES_SMTP_ERROR_SUBJECT

    @property
    def pd_smtp_error_subject(self) -> str:
        return PD_SMTP_ERROR_SUBJECT

    @property
    def sales_op_smtp_error_subject(self) -> str:
        return SALES_OP_SMTP_ERROR_SUBJECT

    @property
    def quotes_nsi_materialized_views(self) -> tuple[str, ...]:
        return _with_default_schema(self.quotes_nsi.mviews_to_refresh, schema_name=self.quotes_nsi_schema)

    @property
    def quotes_nsi_grant_select_users(self) -> tuple[str, ...]:
        return self.quotes_nsi.users_to_grant

    @property
    def quotes_materialized_views(self) -> tuple[str, ...]:
        return _with_default_schema(self.quotes.mviews_to_refresh, schema_name=self.quotes_schema)

    @property
    def quotes_grant_select_users(self) -> tuple[str, ...]:
        return self.quotes.users_to_grant

    @property
    def pd_materialized_views(self) -> tuple[str, ...]:
        return _with_default_schema(self.pd.mviews_to_refresh, schema_name=self.pd_schema)

    @property
    def pd_grant_select_users(self) -> tuple[str, ...]:
        return self.pd.users_to_grant

    @property
    def sales_op_materialized_views(self) -> tuple[str, ...]:
        return _with_default_schema(self.sales_op.mviews_to_refresh, schema_name=self.sales_op_schema)

    @property
    def sales_op_grant_select_users(self) -> tuple[str, ...]:
        return self.sales_op.users_to_grant

    def quotes_nsi_table_schema_reference(self, table_name: str) -> str:
        return f"{self.quotes_nsi_schema}.{table_name}"

    def quotes_table_schema_reference(self, table_name: str) -> str:
        return f"{self.quotes_schema}.{table_name}"

    def pd_table_name(self, table_name: str) -> str:
        return table_name

    def pd_table_schema_reference(self, table_name: str) -> str:
        return f"{self.pd_schema}.{self.pd_table_name(table_name)}"

    def sales_op_table_schema_reference(self, table_name: str) -> str:
        return f"{self.sales_op_schema}.{table_name}"

    def sqlalchemy_url(self, *, database: str | None = None) -> URL:
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.postgres.user,
            password=self.postgres.password,
            host=self.postgres.host,
            port=self.postgres.port,
            database=database or self.postgres.db,
        )


def load_settings() -> Settings:
    path = CONFIG_PATH
    try:
        raw_config = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        settings = Settings.model_validate(raw_config)
        return _apply_env_overrides(settings)
    except OSError as error:
        msg = f"Не удалось прочитать конфигурацию {path}: {error}."
        raise ValueError(msg) from error
    except tomllib.TOMLDecodeError as error:
        msg = f"Ошибка синтаксиса TOML в {path}: {error}."
        raise ValueError(msg) from error
    except ValidationError as error:
        details = "; ".join(_format_validation_error(item) for item in error.errors(include_url=False))
        msg = f"Ошибка чтения настроек из {path}: {details}."
        raise ValueError(msg) from error


def _apply_env_overrides(settings: Settings) -> Settings:
    postgres_host = _strip_optional(environ.get("POSTGRES_HOST"))
    if postgres_host is None:
        return settings

    postgres = settings.common.postgres.model_copy(update={"host": postgres_host})
    common = settings.common.model_copy(update={"postgres": postgres})
    return settings.model_copy(update={"common": common})


def _format_validation_error(error: Mapping[str, object]) -> str:
    location = error.get("loc")
    field_name = ""
    if isinstance(location, Sequence) and location:
        field_name = ".".join(str(part) for part in location)

    if error.get("type") == "missing":
        return f"не заполнена обязательная настройка {field_name}"

    return f"{field_name}: {error.get('msg', 'неизвестная ошибка')}"


def _strip_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _compile_regex(raw_value: str, *, setting_name: str) -> re.Pattern[str]:
    try:
        return re.compile(raw_value, re.IGNORECASE)
    except re.error as error:
        msg = f"{setting_name} содержит некорректное регулярное выражение: {error}."
        raise ValueError(msg) from error


def _normalize_qualified_names(values: tuple[str, ...], *, setting_name: str) -> tuple[str, ...]:
    normalized_values: list[str] = []
    seen_values: set[str] = set()
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            continue

        parts = [part.strip() for part in value.split(".")]
        if len(parts) > MAX_QUALIFIED_NAME_PARTS or any(not part for part in parts):
            msg = f"{setting_name} должен содержать имена в формате name или schema.name."
            raise ValueError(msg)

        normalized_value = ".".join(parts)
        normalized_key = normalized_value.lower()
        if normalized_key in seen_values:
            continue

        seen_values.add(normalized_key)
        normalized_values.append(normalized_value)

    return tuple(normalized_values)


def _with_default_schema(values: tuple[str, ...], *, schema_name: str) -> tuple[str, ...]:
    normalized_values: list[str] = []
    for value in values:
        if "." in value:
            normalized_values.append(value)
        else:
            normalized_values.append(f"{schema_name}.{value}")
    return tuple(normalized_values)


def _validate_imap_settings(settings: Settings) -> None:
    imap_sources = [
        ("quotes_nsi.sources.imap_subject_pattern", settings.quotes_nsi.sources),
        ("quotes.sources.imap_subject_pattern", settings.quotes.sources),
        ("pd.sources.imap_subject_pattern", settings.pd.sources),
        ("sales_op.sources.imap_subject_pattern", settings.sales_op.sources),
    ]
    active_imap_sources = [(name, source) for name, source in imap_sources if source.source == "imap"]
    if not active_imap_sources:
        return

    missing_fields = []
    for setting_name, source in active_imap_sources:
        if source.imap_subject_pattern is None:
            missing_fields.append(setting_name)
    if settings.imap.host is None:
        missing_fields.append("common.imap.host")
    if settings.imap.user is None:
        missing_fields.append("common.imap.user")
    if settings.imap.password is None:
        missing_fields.append("common.imap.password")
    if missing_fields:
        msg = f"Для IMAP-источника нужно заполнить: {', '.join(missing_fields)}."
        raise ValueError(msg)


def _validate_smtp_settings(settings: Settings) -> None:
    if not any(
        (
            settings.quotes_nsi.smtp_error_handling_on,
            settings.quotes.smtp_error_handling_on,
            settings.pd.smtp_error_handling_on,
            settings.sales_op.smtp_error_handling_on,
        ),
    ):
        return

    missing_fields = []
    if settings.smtp.host is None:
        missing_fields.append("common.smtp.host")
    if settings.smtp.port is None:
        missing_fields.append("common.smtp.port")
    if settings.smtp.user is None:
        missing_fields.append("common.smtp.user")
    if settings.smtp.password is None:
        missing_fields.append("common.smtp.password")
    if settings.smtp.from_address is None:
        missing_fields.append("common.smtp.from_address")
    if missing_fields:
        msg = f"Для отправки логов по SMTP нужно заполнить: {', '.join(missing_fields)}."
        raise ValueError(msg)
