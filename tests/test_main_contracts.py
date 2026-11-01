"""
The exit-code/outcome matrix of ``main._run_app`` — the CLI contract for one process run (audit A9).

* module outcomes flow through the real ``_run_modules`` into ``ModuleRun``;
* module errors, format changes, module post-action failures and common
  post-action failures exit with ``1``; warnings alone do not;
* the pushed outcome string follows ``_module_run_outcome``:
  success / partial_success / failure;
* CLI module selection (audit E2): repeated ``--module`` choices unioned with
  the legacy per-module boolean flags, no selection → all modules.

The database seams (``_run_post_actions``, ``_run_common_post_actions``, the
run metrics push) are stubbed, and the module registry is replaced with two
stub entries so no PostgreSQL/IMAP access is needed.
"""

import argparse
import asyncio

import pytest

from config import Settings
from main import (
    MODULE_CODES,
    ModuleEntry,
    ModuleRun,
    ModuleRunner,
    _build_parser,
    _run_app,
    _selected_modules,
)
from module_models import ModuleOutcome

CODES = ("quotes_nsi", "quotes")


def _args() -> argparse.Namespace:
    return argparse.Namespace(quotes_nsi=True, quotes=True, pd=False, sales_op=False, source=None)


def _outcome(
    *, has_errors: bool = False, has_warnings: bool = False, has_format_changes: bool = False
) -> ModuleOutcome:
    return ModuleOutcome(
        has_errors=has_errors,
        has_warnings=has_warnings,
        has_format_changes=has_format_changes,
        tables_to_grant=(),
        relation_references_to_grant=(),
    )


def _stub_runner(outcome: ModuleOutcome) -> ModuleRunner:
    async def runner(_settings: Settings) -> ModuleOutcome:
        return outcome

    return runner


def _run_app_with_matrix(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcomes: tuple[ModuleOutcome, ModuleOutcome],
    failed_post_actions: tuple[str, ...] = (),
    common_post_action_failed: bool = False,
) -> tuple[int, list[str]]:
    """Patch the DB seams, run ``_run_app``, return (exit code, pushed outcomes)."""
    pushed: list[str] = []

    def fake_push_run_outcome_metrics(
        _module_runs: tuple[ModuleRun, ...],
        *,
        outcome: str,
        duration_seconds: float,  # noqa: ARG001  # keyword-only in the real call signature
    ) -> None:
        pushed.append(outcome)

    async def fake_run_post_actions(
        _settings: Settings,
        _module_runs: tuple[ModuleRun, ...],
    ) -> tuple[str, ...]:
        return failed_post_actions

    async def fake_run_common_post_actions(
        _settings: Settings,
        *,
        outcome: str,  # noqa: ARG001  # keyword-only in the real call signature
    ) -> bool:
        return common_post_action_failed

    entries = tuple(
        ModuleEntry(code=code, schema_name="raw", materialized_views=(), grant_users=(), runner=_stub_runner(outcome))
        for code, outcome in zip(CODES, outcomes, strict=True)
    )
    monkeypatch.setattr("main._module_entries", lambda _settings: entries)
    monkeypatch.setattr("main._run_post_actions", fake_run_post_actions)
    monkeypatch.setattr("main._run_common_post_actions", fake_run_common_post_actions)
    monkeypatch.setattr("main._push_run_outcome_metrics", fake_push_run_outcome_metrics)

    exit_code = asyncio.run(_run_app(Settings.model_construct(), args=_args()))
    return exit_code, pushed


def test_run_app_returns_zero_for_all_success(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, pushed = _run_app_with_matrix(monkeypatch, outcomes=(_outcome(), _outcome()))

    assert exit_code == 0
    assert pushed == ["success"]


def test_run_app_warnings_alone_do_not_fail_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, pushed = _run_app_with_matrix(monkeypatch, outcomes=(_outcome(has_warnings=True), _outcome()))

    assert exit_code == 0
    assert pushed == ["success"]


def test_run_app_module_error_yields_exit_1_and_partial_success(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, pushed = _run_app_with_matrix(monkeypatch, outcomes=(_outcome(has_errors=True), _outcome()))

    assert exit_code == 1
    assert pushed == ["partial_success"]


def test_run_app_all_modules_failed_yields_exit_1_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, pushed = _run_app_with_matrix(
        monkeypatch, outcomes=(_outcome(has_errors=True), _outcome(has_errors=True))
    )

    assert exit_code == 1
    assert pushed == ["failure"]


def test_run_app_format_change_yields_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, pushed = _run_app_with_matrix(monkeypatch, outcomes=(_outcome(), _outcome(has_format_changes=True)))

    assert exit_code == 1
    assert pushed == ["partial_success"]


def test_run_app_module_post_action_failure_yields_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, pushed = _run_app_with_matrix(
        monkeypatch,
        outcomes=(_outcome(), _outcome()),
        failed_post_actions=("quotes",),
    )

    assert exit_code == 1
    assert pushed == ["partial_success"]


def test_run_app_common_post_action_failure_yields_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    # The run-level metric is computed from module runs and module post-actions
    # only (`_module_run_outcome`), so a failed common post-action does not
    # change the pushed outcome — that is existing behavior, not the contract
    # under test here. The CLI exit code is the contract.
    exit_code, _pushed = _run_app_with_matrix(
        monkeypatch,
        outcomes=(_outcome(), _outcome()),
        common_post_action_failed=True,
    )

    assert exit_code == 1


# --
# CLI module selection (audit E2): repeated --module choices unioned with the
# legacy per-module boolean flags; no explicit selection means all modules.


def _parsed(argv: list[str]) -> argparse.Namespace:
    """Build a real ``argparse.Namespace`` through ``_build_parser``."""
    return _build_parser().parse_args(argv)


def test_module_flag_selects_the_named_modules() -> None:
    args = _parsed(["--module", "pd", "--module", "quotes"])

    assert _selected_modules(args) == ("pd", "quotes")


def test_module_flag_union_with_legacy_flags() -> None:
    args = _parsed(["--module", "pd", "--quotes"])

    assert _selected_modules(args) == ("pd", "quotes")


def test_legacy_flags_still_select_modules() -> None:
    args = _parsed(["--quotes-nsi", "--source", "local"])

    assert _selected_modules(args) == ("quotes_nsi",)
    assert args.source == "local"


def test_no_selection_defaults_to_all_modules() -> None:
    args = _parsed(["--source", "imap"])

    assert _selected_modules(args) == MODULE_CODES


def test_unknown_module_name_is_rejected_by_argparse() -> None:
    with pytest.raises(SystemExit) as raised:
        _parsed(["--module", "nope"])

    assert raised.value.code == 2
