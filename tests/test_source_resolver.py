"""Local source resolution: path safety, pattern matching and the invalid-regex guard."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from source_resolver import (
    SourceError,
    SourceFile,
    SourceFileNotFoundError,
    _compile_file_pattern,
    _decode_header_text,
    _exact_file_pattern,
    _resolve_folder_source,
    resolve_all_pattern_sources,
    resolve_pattern_sources,
    resolve_source,
)

if TYPE_CHECKING:
    from pathlib import Path

    from config import Settings, SourcesConfig


def _sources(settings: Settings, data_dir: Path) -> SourcesConfig:
    return settings.modules["pd"].sources.model_copy(update={"data_dir": data_dir})


class TestSingleLocalSource:
    def test_relative_path_is_resolved_under_data_dir(self, tmp_path: Path) -> None:
        (tmp_path / "plan.xlsx").write_bytes(b"xlsx")

        source = _resolve_folder_source(data_dir=tmp_path, pattern_or_path="plan.xlsx")

        assert source.path == tmp_path / "plan.xlsx"
        assert source.display_name == str(tmp_path / "plan.xlsx")
        assert source.history_reference == str(tmp_path / "plan.xlsx")

    def test_absolute_path_is_kept(self, tmp_path: Path) -> None:
        path = tmp_path / "absolute.xlsx"
        path.write_bytes(b"xlsx")

        source = _resolve_folder_source(data_dir=tmp_path / "unused", pattern_or_path=path)

        assert source.path == path

    def test_missing_file_raises_the_skip_exception_type(self, tmp_path: Path) -> None:
        with pytest.raises(SourceFileNotFoundError) as raised:
            _resolve_folder_source(data_dir=tmp_path, pattern_or_path="absent.xlsx")

        assert isinstance(raised.value, SourceError)
        assert raised.value.source_path == tmp_path / "absent.xlsx"
        assert str(raised.value).startswith("файл не найден:")

    def test_directory_is_not_a_file(self, tmp_path: Path) -> None:
        (tmp_path / "folder").mkdir()

        with pytest.raises(SourceError, match="источник не является файлом"):
            _resolve_folder_source(data_dir=tmp_path, pattern_or_path="folder")

    def test_resolve_source_local_mode(self, tmp_path: Path, settings: Settings) -> None:
        (tmp_path / "plan.xlsx").write_bytes(b"xlsx")

        with resolve_source(
            settings,
            source_config=_sources(settings, tmp_path),
            data_dir=tmp_path,
            default_pattern_or_path="plan.xlsx",
        ) as source:
            assert source.path.name == "plan.xlsx"


class TestPatternSources:
    def test_fullmatch_case_insensitive_and_casefold_sort(self, tmp_path: Path, settings: Settings) -> None:
        for name in ("B.xlsx", "a.xls", "~$lock.xlsx", "notes.txt"):
            (tmp_path / name).write_bytes(b"x")

        with resolve_pattern_sources(
            settings,
            source_config=_sources(settings, tmp_path),
            data_dir=tmp_path,
            default_file_pattern=r".*\.(xlsx|xls)$",
        ) as sources:
            assert [source.path.name for source in sources] == ["a.xls", "B.xlsx"]

    def test_missing_file_is_a_source_error(self, tmp_path: Path, settings: Settings) -> None:
        with (
            pytest.raises(SourceError, match="не найдено файлов по шаблону"),
            resolve_pattern_sources(
                settings,
                source_config=_sources(settings, tmp_path),
                data_dir=tmp_path,
                default_file_pattern=r".*\.xlsx$",
            ),
        ):
            pass

    def test_missing_data_dir_is_a_source_error(self, tmp_path: Path, settings: Settings) -> None:
        with (
            pytest.raises(SourceError, match="папка источника не найдена"),
            resolve_pattern_sources(
                settings,
                source_config=_sources(settings, tmp_path / "absent"),
                data_dir=tmp_path / "absent",
                default_file_pattern=r".*\.xlsx$",
            ),
        ):
            pass

    def test_allow_empty_returns_no_files(self, tmp_path: Path, settings: Settings) -> None:
        with resolve_all_pattern_sources(
            settings,
            source_config=_sources(settings, tmp_path),
            data_dir=tmp_path,
            default_file_pattern=r".*\.xlsx$",
        ) as sources:
            assert sources == ()

    def test_recursive_local_walks_month_folders(self, tmp_path: Path, settings: Settings) -> None:
        nested = tmp_path / "2026-07" / "sub"
        nested.mkdir(parents=True)
        (nested / "plan.xlsx").write_bytes(b"x")
        (tmp_path / "other.txt").write_bytes(b"x")

        with resolve_all_pattern_sources(
            settings,
            source_config=_sources(settings, tmp_path),
            data_dir=tmp_path,
            default_file_pattern=r".*\.xlsx$",
            recursive_local=True,
        ) as sources:
            assert [source.path.name for source in sources] == ["plan.xlsx"]

    def test_invalid_regex_is_rejected_before_any_enumeration(self, tmp_path: Path, settings: Settings) -> None:
        broken = settings.modules["pd"].sources.model_copy(
            update={"data_dir": tmp_path, "file_pattern": "["},
        )

        with (
            pytest.raises(SourceError, match="некорректное регулярное выражение"),
            resolve_pattern_sources(
                settings,
                source_config=broken,
                data_dir=tmp_path,
                default_file_pattern=r".*\.xlsx$",
            ),
        ):
            pass

    def test_unknown_source_mode_is_rejected(self, tmp_path: Path, settings: Settings) -> None:
        broken = settings.modules["pd"].sources.model_copy(update={"source": "sftp"})

        with (
            pytest.raises(SourceError, match="неизвестный режим источника"),
            resolve_pattern_sources(
                settings,
                source_config=broken,
                data_dir=tmp_path,
                default_file_pattern=r".*\.xlsx$",
            ),
        ):
            pass


class TestHelpers:
    def test_compile_file_pattern_is_case_insensitive(self) -> None:
        pattern = _compile_file_pattern(r"plan\.xlsx$")

        assert pattern.fullmatch("PLAN.XLSX") is not None
        assert pattern.fullmatch("plan_xlsx") is None

    def test_invalid_pattern_guard(self) -> None:
        with pytest.raises(SourceError, match="некорректное регулярное выражение"):
            _compile_file_pattern("(")

    def test_history_reference_distinguishes_imap_attachments(self, tmp_path: Path) -> None:
        local_path = tmp_path / "pd.xlsx"
        imap_path = tmp_path / "attachment" / "pd.xlsx"
        local = SourceFile(path=local_path, display_name=str(local_path))
        imap = SourceFile(
            path=imap_path,
            display_name="IMAP: Report / pd.xlsx",
            original_name="pd.xlsx",
        )
        imap_named_without_prefix = SourceFile(
            path=imap_path,
            display_name="pd.xlsx",
            original_name="pd.xlsx",
        )

        assert local.history_reference == str(local_path)
        assert imap.history_reference == "imap/pd.xlsx"
        assert imap_named_without_prefix.history_reference == str(imap_named_without_prefix.path)

    def test_exact_file_pattern_escapes_the_file_name(self) -> None:
        assert _exact_file_pattern("data/plan.xlsx") == r"plan\.xlsx"

    def test_encoded_imap_header_is_decoded_without_a_connection(self) -> None:
        assert _decode_header_text("=?utf-8?b?0J/RgNC40LLQtdGC?=") == "Привет"
        assert _decode_header_text("plain subject") == "plain subject"
