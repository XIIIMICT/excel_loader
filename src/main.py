import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from app_logging import configure_logging, get_logger, reset_log_module, set_log_module
from config import MODULE_CODES, Settings, load_settings
from db import postgres_session
from dictionaries.quotes_nsi_registry import DICTIONARIES as QUOTES_NSI_DICTIONARIES
from dictionaries.quotes_registry import COMBINED_DICTIONARIES as QUOTES_COMBINED_DICTIONARIES
from dictionaries.quotes_registry import DICTIONARIES as QUOTES_DICTIONARIES
from dictionaries.quotes_registry import PATTERN_DICTIONARIES as QUOTES_PATTERN_DICTIONARIES
from dictionary_runner import run_dictionaries
from metrics import build_run_metrics, push_run_metrics
from module_models import ModuleOutcome
from pd.runner import run_pd
from postgres import (
    grant_select_on_relation_to_existing_roles,
    grant_select_on_table_to_existing_roles,
    grant_usage_on_schema_to_existing_roles,
    refresh_existing_materialized_views,
)
from sales_op.models import SalesOpRunOptions
from sales_op.runner import parse_sales_op_options, run_sales_op

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine

    from config import ModuleConfig

LOGGER = get_logger("main")

type ModuleRunner = Callable[[Settings], Awaitable[ModuleOutcome]]


@dataclass(frozen=True, slots=True)
class ModuleEntry:
    """
    Static per-module post-action config plus the runner that produces its outcome.

    Adding a module is one ``ModuleEntry`` in ``_module_entries``, one line in
    ``MODULE_CODES`` plus a runner function returning ``ModuleOutcome``;
    ``_run_modules`` itself is unchanged.
    """

    code: str
    schema_name: str
    materialized_views: tuple[str, ...]
    grant_users: tuple[str, ...]
    runner: ModuleRunner


@dataclass(frozen=True, slots=True)
class ModuleRun:
    code: str
    schema_name: str
    has_errors: bool
    has_warnings: bool
    has_format_changes: bool
    tables_to_grant: tuple[Table, ...]
    relation_references_to_grant: tuple[str, ...]
    materialized_views: tuple[str, ...]
    grant_users: tuple[str, ...]


def main() -> int:
    args = _build_parser().parse_args()
    try:
        settings = _apply_cli_overrides(load_settings(), args=args)
        sales_op_options = _sales_op_run_options(args)
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 2

    started_at = datetime.now(tz=settings.app_timezone)
    configure_logging(settings, started_at)
    return asyncio.run(_run_app(settings, args=args, sales_op_options=sales_op_options))


async def _run_app(
    settings: Settings,
    *,
    args: argparse.Namespace,
    sales_op_options: SalesOpRunOptions | None = None,
) -> int:
    run_started_at = time.monotonic()
    module_runs = await _run_modules(
        settings,
        _selected_modules(args),
        sales_op_options=sales_op_options,
    )
    post_action_failed_modules = await _run_post_actions(settings, module_runs)
    outcome = _module_run_outcome(module_runs, post_action_failed_modules=post_action_failed_modules)
    common_post_action_failed = await _run_common_post_actions(settings, outcome=outcome)
    # The loader no longer e-mails errors: alerts are delivered by the
    # monitoring stack (Alertmanager, see docs/ARCHITECTURE.md). If the stack
    # is unreachable, the metrics push below logs the failure to the log file.
    exit_code = (
        1
        if common_post_action_failed
        or post_action_failed_modules
        or any(module_run.has_errors or module_run.has_format_changes for module_run in module_runs)
        else 0
    )
    _push_run_outcome_metrics(
        module_runs,
        outcome=outcome,
        duration_seconds=time.monotonic() - run_started_at,
    )
    return exit_code


def _push_run_outcome_metrics(
    module_runs: tuple[ModuleRun, ...],
    *,
    outcome: str,
    duration_seconds: float,
) -> None:
    module_failed_set = {
        module_run.code for module_run in module_runs if module_run.has_errors or module_run.has_format_changes
    }
    push_run_metrics(
        build_run_metrics(
            outcome,
            duration_seconds=duration_seconds,
            module_codes=[m.code for m in module_runs],
            failed_modules=module_failed_set,
        ),
    )


def _module_run_outcome(
    module_runs: tuple[ModuleRun, ...],
    *,
    post_action_failed_modules: tuple[str, ...],
) -> str:
    failed_modules = {
        module_run.code for module_run in module_runs if module_run.has_errors or module_run.has_format_changes
    }
    failed_modules.update(post_action_failed_modules)
    if not failed_modules:
        return "success"
    if len(failed_modules) < len(module_runs):
        return "partial_success"
    return "failure"


async def _run_common_post_actions(settings: Settings, *, outcome: str) -> bool:
    selected_actions = [
        action for action in settings.common.post_actions if "always" in action.run_on or outcome in action.run_on
    ]
    if not selected_actions:
        return False

    has_errors = False
    async with postgres_session(settings) as engine:
        for action in selected_actions:
            try:
                sql = await asyncio.to_thread(action.path.read_text, encoding="utf-8")
                sql = _render_post_action_sql(sql, settings=settings)
                async with engine.begin() as connection:
                    await connection.exec_driver_sql(sql)
            except Exception:
                LOGGER.exception("Не удалось выполнить общее пост-действие %s.", action.path)
                has_errors = True
                continue
            LOGGER.info("Общее пост-действие %s выполнено для состояния %s.", action.path, outcome)
    return has_errors


SOURCE_OWNER_PLACEHOLDER = "__SOURCE_OWNER__"


def _render_post_action_sql(sql: str, *, settings: Settings) -> str:
    # The single templated value today is the PostgreSQL role the loader
    # connects as (``common.postgres.user`` / ``POSTGRES_USER``): the owner
    # whose objects ``assign_object_owner.sql`` reassigns to ``object_owner``.
    # Bound parameters cannot cross a ``DO $$ ... $$`` body, so the value is
    # injected here as an escaped string literal. A post-action without the
    # placeholder is returned unchanged.
    return sql.replace(SOURCE_OWNER_PLACEHOLDER, _sql_string_literal(settings.postgres_user))


def _sql_string_literal(value: str) -> str:
    # PostgreSQL string literal: wrap in single quotes, double internal quotes.
    return "'" + value.replace("'", "''") + "'"


def _module_entries(
    settings: Settings,
    *,
    sales_op_options: SalesOpRunOptions | None = None,
) -> tuple[ModuleEntry, ...]:
    """
    Return the module registry, one entry per module, ordered as modules run.

    Adding module #5 is a new ``ModuleEntry`` here plus a runner returning
    ``ModuleOutcome``. ``_run_modules`` consumes this tuple and never needs
    editing for a new module.
    """
    options = sales_op_options or SalesOpRunOptions()

    async def sales_op_runner(runner_settings: Settings) -> ModuleOutcome:
        return await run_sales_op(runner_settings, options=options)

    return (
        ModuleEntry(
            code="quotes_nsi",
            schema_name=settings.modules["quotes_nsi"].schema_name,
            materialized_views=settings.modules["quotes_nsi"].materialized_views,
            grant_users=settings.modules["quotes_nsi"].users_to_grant,
            runner=_run_quotes_nsi,
        ),
        ModuleEntry(
            code="quotes",
            schema_name=settings.modules["quotes"].schema_name,
            materialized_views=settings.modules["quotes"].materialized_views,
            grant_users=settings.modules["quotes"].users_to_grant,
            runner=_run_quotes,
        ),
        ModuleEntry(
            code="pd",
            schema_name=settings.modules["pd"].schema_name,
            materialized_views=settings.modules["pd"].materialized_views,
            grant_users=settings.modules["pd"].users_to_grant,
            runner=run_pd,
        ),
        ModuleEntry(
            code="sales_op",
            schema_name=settings.modules["sales_op"].schema_name,
            materialized_views=settings.modules["sales_op"].materialized_views,
            grant_users=settings.modules["sales_op"].users_to_grant,
            runner=sales_op_runner,
        ),
    )


async def _run_modules(
    settings: Settings,
    modules: tuple[str, ...],
    *,
    sales_op_options: SalesOpRunOptions | None = None,
) -> tuple[ModuleRun, ...]:
    entries = {entry.code: entry for entry in _module_entries(settings, sales_op_options=sales_op_options)}
    module_runs: list[ModuleRun] = []
    for code in modules:
        entry = entries[code]
        token = set_log_module(code)
        try:
            outcome = await entry.runner(settings)
        finally:
            reset_log_module(token)

        module_runs.append(
            ModuleRun(
                code=code,
                schema_name=entry.schema_name,
                has_errors=outcome.has_errors,
                has_warnings=outcome.has_warnings,
                has_format_changes=outcome.has_format_changes,
                tables_to_grant=outcome.tables_to_grant,
                relation_references_to_grant=outcome.relation_references_to_grant,
                materialized_views=entry.materialized_views,
                grant_users=entry.grant_users,
            ),
        )

    return tuple(module_runs)


async def _run_quotes_nsi(settings: Settings) -> ModuleOutcome:
    return await run_dictionaries(
        settings,
        module_label="НСИ котировок",
        module_config=settings.modules["quotes_nsi"],
        dictionary_loaders=QUOTES_NSI_DICTIONARIES,
    )


async def _run_quotes(settings: Settings) -> ModuleOutcome:
    return await run_dictionaries(
        settings,
        module_label="Котировки",
        module_config=settings.modules["quotes"],
        dictionary_loaders=QUOTES_DICTIONARIES,
        combined_loaders=QUOTES_COMBINED_DICTIONARIES,
        pattern_loaders=QUOTES_PATTERN_DICTIONARIES,
    )


async def _run_post_actions(settings: Settings, module_runs: tuple[ModuleRun, ...]) -> tuple[str, ...]:
    failed_modules: list[str] = []
    async with postgres_session(settings) as engine:
        for module_run in module_runs:
            if module_run.has_errors:
                LOGGER.info(
                    "Пост-действия для модуля %s пропущены, так как модуль завершился с ошибками.",
                    module_run.code,
                )
                continue

            module_has_errors = await _grant_access_if_needed(module_run, engine=engine)
            module_has_errors = (
                await _refresh_materialized_views_if_needed(module_run, engine=engine) or module_has_errors
            )
            if module_has_errors:
                failed_modules.append(module_run.code)

    return tuple(failed_modules)


async def _grant_access_if_needed(module_run: ModuleRun, *, engine: AsyncEngine) -> bool:
    if not module_run.grant_users:
        return False

    has_errors = await _grant_schema_usage(module_run, engine=engine)
    for table in module_run.tables_to_grant:
        try:
            granted_roles, skipped_roles = await grant_select_on_table_to_existing_roles(
                engine,
                table=table,
                role_names=module_run.grant_users,
            )
        except Exception:
            LOGGER.exception("Не удалось выдать SELECT на таблицу %s.%s.", table.schema, table.name)
            has_errors = True
            continue

        _log_grant_result(
            permission="SELECT",
            target=f"{table.schema}.{table.name}",
            granted_roles=granted_roles,
            skipped_roles=skipped_roles,
        )

    for relation_reference in module_run.relation_references_to_grant:
        try:
            granted_roles, skipped_roles = await grant_select_on_relation_to_existing_roles(
                engine,
                relation_reference=relation_reference,
                role_names=module_run.grant_users,
            )
        except Exception:
            LOGGER.exception("Не удалось выдать SELECT на отношение %s.", relation_reference)
            has_errors = True
            continue

        _log_grant_result(
            permission="SELECT",
            target=relation_reference,
            granted_roles=granted_roles,
            skipped_roles=skipped_roles,
        )

    return has_errors


async def _grant_schema_usage(module_run: ModuleRun, *, engine: AsyncEngine) -> bool:
    try:
        granted_roles, skipped_roles = await grant_usage_on_schema_to_existing_roles(
            engine,
            schema_name=module_run.schema_name,
            role_names=module_run.grant_users,
        )
    except Exception:
        LOGGER.exception("Не удалось выдать USAGE на схему %s.", module_run.schema_name)
        return True

    _log_grant_result(
        permission="USAGE",
        target=f"схему {module_run.schema_name}",
        granted_roles=granted_roles,
        skipped_roles=skipped_roles,
    )
    return False


def _log_grant_result(
    *,
    permission: str,
    target: str,
    granted_roles: tuple[str, ...],
    skipped_roles: tuple[str, ...],
) -> None:
    if granted_roles:
        LOGGER.info("Выданы права %s на %s пользователям: %s.", permission, target, ", ".join(granted_roles))
    if skipped_roles:
        LOGGER.info(
            "Пользователи не найдены и пропущены при выдаче %s на %s: %s.",
            permission,
            target,
            ", ".join(skipped_roles),
        )


async def _refresh_materialized_views_if_needed(module_run: ModuleRun, *, engine: AsyncEngine) -> bool:
    if not module_run.materialized_views:
        return False

    try:
        refreshed_views, skipped_views = await refresh_existing_materialized_views(
            engine,
            view_references=module_run.materialized_views,
        )
    except Exception:
        LOGGER.exception("Не удалось обновить материализованные представления для модуля %s.", module_run.code)
        return True

    if refreshed_views:
        LOGGER.info(
            "Материализованные представления обновлены для модуля %s: %s.",
            module_run.code,
            ", ".join(refreshed_views),
        )
    if skipped_views:
        LOGGER.info(
            "Материализованные представления не найдены и пропущены для модуля %s: %s.",
            module_run.code,
            ", ".join(skipped_views),
        )
    return False


def _selected_modules(args: argparse.Namespace) -> tuple[str, ...]:
    """
    Modules to run.

    Repeated ``--module`` choices unioned with the legacy per-module boolean
    flags; no explicit selection means all modules.
    """
    selected_modules: list[str] = []
    for code in getattr(args, "modules", None) or ():
        if code not in selected_modules:
            selected_modules.append(code)
    for code in MODULE_CODES:
        if getattr(args, code, False) and code not in selected_modules:
            selected_modules.append(code)
    return tuple(selected_modules) or MODULE_CODES


def _apply_cli_overrides(settings: Settings, *, args: argparse.Namespace) -> Settings:
    updates: dict[str, ModuleConfig] = {}
    if args.source is not None:
        for code in _selected_modules(args):
            module = settings.modules[code]
            updates[code] = module.model_copy(
                update={"sources": module.sources.model_copy(update={"source": args.source})},
            )
    if not updates:
        return settings
    modules = dict(settings.modules)
    modules.update(updates)
    return settings.model_copy(update={"modules": modules})


def _sales_op_run_options(args: argparse.Namespace) -> SalesOpRunOptions | None:
    """Parse the sales_op-only flags; ``None`` means the default (current month)."""
    force = bool(getattr(args, "force", False))
    months = getattr(args, "months", None)
    if not force and months is None:
        return None
    if "sales_op" not in _selected_modules(args):
        msg = "Флаги --force и --months применимы только к модулю sales_op."
        raise ValueError(msg)
    return parse_sales_op_options(force=force, months=months)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Загрузка НСИ котировок, котировок, ПД и плана продаж.")
    parser.add_argument(
        "--module",
        dest="modules",
        action="append",
        choices=MODULE_CODES,
        help="Загрузить выбранный модуль (можно повторять; объединяется с флагами ниже).",
    )
    parser.add_argument("--quotes-nsi", dest="quotes_nsi", action="store_true", help="Загрузить только НСИ котировок.")
    parser.add_argument("--quotes", action="store_true", help="Загрузить только котировки.")
    parser.add_argument("--pd", action="store_true", help="Загрузить только производственную сводку.")
    parser.add_argument("--sales-op", dest="sales_op", action="store_true", help="Загрузить только план продаж.")
    parser.add_argument(
        "--source",
        choices=("local", "imap"),
        help="Переопределить источник для выбранных модулей.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="План продаж: загрузить и файлы месяца, начинающиеся с «_».",
    )
    parser.add_argument(
        "--months",
        metavar="МЕСЯЦЫ",
        help="План продаж: папки месяцев через запятую (2026-01,2026-02) или all.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
