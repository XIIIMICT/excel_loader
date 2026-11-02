"""Config-model tests: validators, qualified-name normalization, `load_settings` error paths, POSTGRES_HOST override."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

import config
from config import MODULE_CODES


def _module(schema: str, *, data_dir: str = "data") -> dict[str, object]:
    return {"schema": schema, "sources": {"data_dir": data_dir}}


def _settings_data() -> dict[str, object]:
    return {
        "quotes_nsi": _module("raw"),
        "quotes": _module("raw"),
        "pd": _module("pd"),
        "sales_op": _module("raw"),
    }


def _minimal_settings() -> config.Settings:
    return config.Settings.model_validate(_settings_data())


def _settings_toml() -> str:
    lines = []
    for code in MODULE_CODES:
        lines.append(f"[{code}]")
        lines.append('schema = "raw"')
        lines.append(f"[{code}.sources]")
        lines.append('data_dir = "data"')
    return "\n".join(lines) + "\n"


# --- module models -----------------------------------------------------------


def test_sources_data_dir_resolves_relative_to_project_dir() -> None:
    sources = config.SourcesConfig.model_validate({"data_dir": "data/nested"})

    assert sources.data_dir == config.PROJECT_DIR / "data" / "nested"


def test_sources_data_dir_keeps_absolute_paths() -> None:
    absolute = Path("D:/data/absolute").absolute()

    sources = config.SourcesConfig.model_validate({"data_dir": absolute})

    assert sources.data_dir == absolute


def test_sources_rejects_unknown_source_mode() -> None:
    with pytest.raises(ValidationError, match="source должен быть 'local' или 'imap'"):
        config.SourcesConfig.model_validate({"data_dir": "data", "source": "ftp"})


def test_compiled_file_pattern_matches_case_insensitive() -> None:
    sources = config.SourcesConfig.model_validate({"data_dir": "data", "file_pattern": ".*\\.xlsx$"})

    assert sources.compiled_file_pattern.match("Report.XLSX") is not None


def test_compiled_imap_subject_pattern_raises_when_not_configured() -> None:
    sources = config.SourcesConfig.model_validate({"data_dir": "data"})

    with pytest.raises(ValueError, match="imap_subject_pattern не настроен"):
        _ = sources.compiled_imap_subject_pattern


def test_sources_rejects_invalid_regex() -> None:
    with pytest.raises(ValidationError, match="некорректное регулярное выражение"):
        config.SourcesConfig.model_validate({"data_dir": "data", "file_pattern": "("})


def test_imap_optional_text_is_stripped_or_cleared() -> None:
    imap = config.ImapConfig.model_validate({"host": "  mail.corp  ", "user": " ", "password": "\t"})

    assert imap.host == "mail.corp"
    assert imap.user is None
    assert imap.password is None


def test_post_action_run_on_requires_a_mode_and_deduplicates() -> None:
    action = config.PostActionConfig.model_validate({"path": "sql/x.sql", "run_on": ["success", "success"]})

    assert action.run_on == ("success",)
    assert action.path == config.PROJECT_DIR / "sql" / "x.sql"

    with pytest.raises(ValidationError, match="хотя бы одно состояние run_on"):
        config.PostActionConfig.model_validate({"path": "sql/x.sql", "run_on": []})


def test_common_timezone_rejects_unknown_zone() -> None:
    with pytest.raises(ValidationError, match="Неизвестная временная зона"):
        config.CommonConfig.model_validate({"timezone": "Mars/Olympus"})


def test_mviews_reject_qualified_names_deeper_than_two_parts() -> None:
    with pytest.raises(ValidationError, match=r"name или schema\.name"):
        config.ModuleBaseConfig.model_validate({"schema": "raw", "mviews_to_refresh": ["a.b.c"]})


def test_mviews_reject_empty_name_parts() -> None:
    with pytest.raises(ValidationError, match=r"name или schema\.name"):
        config.ModuleBaseConfig.model_validate({"schema": "raw", "mviews_to_refresh": ["raw..view"]})


def test_mviews_normalize_case_insensitive_duplicates() -> None:
    module = config.ModuleBaseConfig.model_validate({"schema": "raw", "mviews_to_refresh": ["Raw.A", "raw.A"]})

    assert module.mviews_to_refresh == ("Raw.A",)


def test_common_directory_paths_resolve_relative_to_project_dir() -> None:
    common = config.CommonConfig.model_validate({"log_dir": "my_logs"})

    assert common.log_dir == config.PROJECT_DIR / "my_logs"


# --- settings ----------------------------------------------------------------


def test_settings_schema_table_reference_helpers() -> None:
    settings = _minimal_settings()

    assert settings.modules["quotes_nsi"].table_reference("t1") == "raw.t1"
    assert settings.modules["quotes"].table_reference("t1") == "raw.t1"
    assert settings.modules["pd"].table_reference("pd_data") == "pd.pd_data"
    assert settings.modules["sales_op"].table_reference("sales_op") == "raw.sales_op"


def test_settings_collects_flat_module_blocks_into_modules() -> None:
    settings = _minimal_settings()

    assert tuple(settings.modules) == MODULE_CODES
    assert settings.modules["pd"].schema_name == "pd"


def test_settings_accepts_direct_modules_mapping() -> None:
    data = {
        "common": {"timezone": "UTC"},
        "modules": {code: _module("raw") for code in MODULE_CODES},
    }

    settings = config.Settings.model_validate(data)

    assert tuple(settings.modules) == MODULE_CODES
    assert settings.modules["quotes_nsi"].schema_name == "raw"


def test_settings_rejects_module_blocks_alongside_modules() -> None:
    data = {"modules": {code: _module("raw") for code in MODULE_CODES}, "quotes": _module("raw")}

    with pytest.raises(ValidationError, match="extra_forbidden"):
        config.Settings.model_validate(data)


def test_imap_validation_walks_modules() -> None:
    data = _settings_data()
    data["quotes"] = _module("raw")
    data["quotes"]["sources"] = {"data_dir": "data", "source": "imap"}
    data["common"] = {}

    with pytest.raises(ValueError, match=r"Для IMAP-источника нужно заполнить"):
        config.Settings.model_validate(data)


def test_settings_app_timezone_follows_common_timezone() -> None:
    data = _settings_data()
    data["common"] = {"timezone": "UTC"}

    settings = config.Settings.model_validate(data)

    assert settings.app_timezone == ZoneInfo("UTC")


def test_sqlalchemy_url_defaults_and_override() -> None:
    """The URL defaults mirror the documented defaults in `config.example.toml`."""
    settings = _minimal_settings()

    default_url = settings.sqlalchemy_url()
    override_url = settings.sqlalchemy_url(database="postgres")

    assert default_url.drivername == "postgresql+asyncpg"
    assert default_url.host == "localhost"
    assert default_url.port == 5432
    assert default_url.username == "postgres"
    assert default_url.password == "strong_password"  # noqa: S105  # это задокументированный дефолт из config.py
    assert default_url.database == "dwh"
    assert override_url.database == "postgres"


def test_sqlalchemy_url_requires_host_and_credentials() -> None:
    data = _settings_data()
    data["common"] = {"postgres": {"host": "db.corp", "port": 6543, "user": "etl", "password": "p", "db": "dwh"}}

    settings = config.Settings.model_validate(data)

    url = settings.sqlalchemy_url()
    assert url.host == "db.corp"
    assert url.port == 6543
    assert url.username == "etl"
    assert url.database == "dwh"


# --- load_settings -----------------------------------------------------------


def test_load_settings_missing_file_raises_value_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "absent.toml")

    with pytest.raises(ValueError, match="Не удалось прочитать конфигурацию"):
        config.load_settings()


def test_load_settings_invalid_toml_raises_value_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "broken.toml"
    config_path.write_text("[quotes_nsi]\nschema = ", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    with pytest.raises(ValueError, match="Ошибка синтаксиса TOML"):
        config.load_settings()


def test_load_settings_missing_required_module_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "partial.toml"
    config_path.write_text("[quotes_nsi]\nschema = 'raw'\n[quotes_nsi.sources]\ndata_dir = 'data'\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    with pytest.raises(ValueError, match="Конфигурация не содержит модулей"):
        config.load_settings()


def test_load_settings_invalid_settings_raise_value_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "invalid.toml"
    config_path.write_text(
        "[quotes_nsi]\nschema = 'raw'\n[quotes_nsi.sources]\ndata_dir = 'data'\nsource = 'ftp'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    with pytest.raises(ValueError, match="Ошибка чтения настроек"):
        config.load_settings()


def test_load_settings_resolves_valid_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_settings_toml(), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    settings = config.load_settings()

    assert settings.modules["quotes_nsi"].sources.data_dir == config.PROJECT_DIR / "data"
    assert settings.postgres.host == "localhost"


def test_load_settings_applies_postgres_host_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_settings_toml(), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    monkeypatch.setenv("POSTGRES_HOST", "db.override.corp")

    settings = config.load_settings()

    assert settings.postgres.host == "db.override.corp"
