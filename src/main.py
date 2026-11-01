import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from app_logging import configure_logging, get_logger, reset_log_module, set_log_module
from config import Settings, load_settings
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
from sales_op.runner import run_sales_op

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine

LOGGER = get_logger("main")

type ModuleRunner = Callable[[Settings], Awaitable[ModuleOutcome]]


@dataclass(frozen=True, slots=True)
class ModuleEntry:
    """
    Static per-module post-action config plus the runner that produces its outcome.

    Adding a module is one ``ModuleEntry`` in ``_module_entries`` plus a runner
    function returning ``ModuleOutcome``; ``_run_modules`` itself is unchanged.
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
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 2

    started_at = datetime.now(tz=settings.app_timezone)
    configure_logging(settings, started_at)
    return asyncio.run(_run_app(settings, args=args))


async def _run_app(settings: Settings, *, args: argparse.Namespace) -> int:
    run_started_at = time.monotonic()
    module_runs = await _run_modules(settings, _selected_modules(args))
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
                async with engine.begin() as connection:
                    await connection.exec_driver_sql(sql)
            except Exception:
                LOGGER.exception("Не удалось выполнить общее пост-действие %s.", action.path)
                has_errors = True
                continue
            LOGGER.info("Общее пост-действие %s выполнено для состояния %s.", action.path, outcome)
    return has_errors


def _module_entries(settings: Settings) -> tuple[ModuleEntry, ...]:
    """
    Return the module registry, one entry per module, ordered as modules run.

    Adding module #5 is a new ``ModuleEntry`` here plus a runner returning
    ``ModuleOutcome``. ``_run_modules`` consumes this tuple and never needs
    editing for a new module.
    """
    return (
        ModuleEntry(
            code="quotes_nsi",
            schema_name=settings.quotes_nsi_schema,
            materialized_views=settings.quotes_nsi_materialized_views,
            grant_users=settings.quotes_nsi_grant_select_users,
            runner=_run_quotes_nsi,
        ),
        ModuleEntry(
            code="quotes",
            schema_name=settings.quotes_schema,
            materialized_views=settings.quotes_materialized_views,
            grant_users=settings.quotes_grant_select_users,
            runner=_run_quotes,
        ),
        ModuleEntry(
            code="pd",
            schema_name=settings.pd_schema,
            materialized_views=settings.pd_materialized_views,
            grant_users=settings.pd_grant_select_users,
            runner=run_pd,
        ),
        ModuleEntry(
            code="sales_op",
            schema_name=settings.sales_op_schema,
            materialized_views=settings.sales_op_materialized_views,
            grant_users=settings.sales_op_grant_select_users,
            runner=run_sales_op,
        ),
    )


async def _run_modules(settings: Settings, modules: tuple[str, ...]) -> tuple[ModuleRun, ...]:
    entries = {entry.code: entry for entry in _module_entries(settings)}
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
        module_config=settings.quotes_nsi,
        dictionary_loaders=QUOTES_NSI_DICTIONARIES,
    )


async def _run_quotes(settings: Settings) -> ModuleOutcome:
    return await run_dictionaries(
        settings,
        module_label="Котировки",
        module_config=settings.quotes,
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
    selected_modules: list[str] = []
    if args.quotes_nsi:
        selected_modules.append("quotes_nsi")
    if args.quotes:
        selected_modules.append("quotes")
    if args.pd:
        selected_modules.append("pd")
    if args.sales_op:
        selected_modules.append("sales_op")
    if selected_modules:
        return tuple(selected_modules)
    return "quotes_nsi", "quotes", "pd", "sales_op"


def _apply_cli_overrides(settings: Settings, *, args: argparse.Namespace) -> Settings:
    modules = _selected_modules(args)
    quotes_nsi = settings.quotes_nsi
    quotes = settings.quotes
    pd = settings.pd
    sales_op = settings.sales_op

    if args.source is not None:
        if "quotes_nsi" in modules:
            quotes_nsi = quotes_nsi.model_copy(
                update={"sources": quotes_nsi.sources.model_copy(update={"source": args.source})},
            )
        if "quotes" in modules:
            quotes = quotes.model_copy(update={"sources": quotes.sources.model_copy(update={"source": args.source})})
        if "pd" in modules:
            pd = pd.model_copy(update={"sources": pd.sources.model_copy(update={"source": args.source})})
        if "sales_op" in modules:
            sales_op = sales_op.model_copy(
                update={"sources": sales_op.sources.model_copy(update={"source": args.source})},
            )

    return settings.model_copy(
        update={"quotes_nsi": quotes_nsi, "quotes": quotes, "pd": pd, "sales_op": sales_op},
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Загрузка НСИ котировок, котировок, ПД и плана продаж.")
    parser.add_argument("--quotes-nsi", dest="quotes_nsi", action="store_true", help="Загрузить только НСИ котировок.")
    parser.add_argument("--quotes", action="store_true", help="Загрузить только котировки.")
    parser.add_argument("--pd", action="store_true", help="Загрузить только производственную сводку.")
    parser.add_argument("--sales-op", dest="sales_op", action="store_true", help="Загрузить только план продаж.")
    parser.add_argument(
        "--source",
        choices=("local", "imap"),
        help="Переопределить источник для выбранных модулей.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
