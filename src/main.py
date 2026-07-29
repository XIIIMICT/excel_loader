import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from aiosmtplib.errors import SMTPAuthenticationError

from app_logging import configure_logging, flush_log_handlers, get_logger, reset_log_module, set_log_module
from config import Settings, load_settings
from db import create_postgres_engine
from dictionaries.quotes_nsi_registry import DICTIONARIES as QUOTES_NSI_DICTIONARIES
from dictionaries.quotes_registry import COMBINED_DICTIONARIES as QUOTES_COMBINED_DICTIONARIES
from dictionaries.quotes_registry import DICTIONARIES as QUOTES_DICTIONARIES
from dictionary_runner import DictionaryRunSummary, run_dictionaries
from email_sender import send_log_email
from pd.runner import PdRunSummary, run_pd
from pd.schema import build_pd_tables, pd_view_reference
from postgres import (
    grant_select_on_relation_to_existing_roles,
    grant_select_on_table_to_existing_roles,
    grant_usage_on_schema_to_existing_roles,
    refresh_existing_materialized_views,
)
from sales_op.runner import SalesOpRunSummary, run_sales_op

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine

LOGGER = get_logger("main")


@dataclass(frozen=True, slots=True)
class ModuleRun:
    code: str
    schema_name: str
    has_errors: bool
    has_warnings: bool
    has_format_changes: bool
    smtp_error_handling_on: bool
    smtp_recipients: tuple[str, ...]
    smtp_subject: str
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
    log_path = configure_logging(settings, started_at)
    return asyncio.run(_run_app(settings, args=args, log_path=log_path))


async def _run_app(settings: Settings, *, args: argparse.Namespace, log_path: Path) -> int:
    module_runs = await _run_modules(settings, _selected_modules(args))
    post_action_failed_modules = await _run_post_actions(settings, module_runs)
    outcome = _module_run_outcome(module_runs, post_action_failed_modules=post_action_failed_modules)
    common_post_action_failed = await _run_common_post_actions(settings, outcome=outcome)
    email_failed = await _send_error_emails(
        settings,
        module_runs=module_runs,
        post_action_failed_modules=post_action_failed_modules,
        log_path=log_path,
        common_post_action_failed=common_post_action_failed,
    )
    return (
        1
        if email_failed
        or common_post_action_failed
        or post_action_failed_modules
        or any(module_run.has_errors or module_run.has_format_changes for module_run in module_runs)
        else 0
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

    engine = create_postgres_engine(settings)
    has_errors = False
    try:
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
    finally:
        await engine.dispose()
    return has_errors


async def _run_modules(settings: Settings, modules: tuple[str, ...]) -> tuple[ModuleRun, ...]:
    module_runs: list[ModuleRun] = []
    for module in modules:
        token = set_log_module(module)
        try:
            if module == "quotes_nsi":
                module_runs.append(await _run_dictionary_module(settings, module_code=module))
            if module == "quotes":
                module_runs.append(await _run_dictionary_module(settings, module_code=module))
            if module == "pd":
                module_runs.append(await _run_pd_module(settings))
            if module == "sales_op":
                module_runs.append(await _run_sales_op_module(settings))
        finally:
            reset_log_module(token)

    return tuple(module_runs)


async def _run_dictionary_module(settings: Settings, *, module_code: str) -> ModuleRun:
    is_quotes_nsi = module_code == "quotes_nsi"
    module_config = settings.quotes_nsi if is_quotes_nsi else settings.quotes
    summary: DictionaryRunSummary = await run_dictionaries(
        settings,
        module_label="НСИ котировок" if is_quotes_nsi else "Котировки",
        module_config=module_config,
        dictionary_loaders=QUOTES_NSI_DICTIONARIES if is_quotes_nsi else QUOTES_DICTIONARIES,
        combined_loaders=() if is_quotes_nsi else QUOTES_COMBINED_DICTIONARIES,
    )
    return ModuleRun(
        code=module_code,
        schema_name=module_config.schema_name,
        has_errors=summary.has_errors,
        has_warnings=summary.has_warnings,
        has_format_changes=False,
        smtp_error_handling_on=module_config.smtp_error_handling_on,
        smtp_recipients=module_config.smtp_recipients,
        smtp_subject=(settings.quotes_nsi_smtp_error_subject if is_quotes_nsi else settings.quotes_smtp_error_subject),
        tables_to_grant=summary.loaded_tables,
        relation_references_to_grant=(),
        materialized_views=(
            settings.quotes_nsi_materialized_views if is_quotes_nsi else settings.quotes_materialized_views
        ),
        grant_users=module_config.users_to_grant,
    )


async def _run_pd_module(settings: Settings) -> ModuleRun:
    summary: PdRunSummary = await run_pd(settings)
    tables = build_pd_tables(settings)
    return ModuleRun(
        code="pd",
        schema_name=settings.pd_schema,
        has_errors=summary.has_errors,
        has_warnings=False,
        has_format_changes=summary.has_format_changes,
        smtp_error_handling_on=settings.pd.smtp_error_handling_on,
        smtp_recipients=settings.pd.smtp_recipients,
        smtp_subject=settings.pd_smtp_error_subject,
        tables_to_grant=(tables.assets, tables.materials, tables.indicators, tables.data, tables.change_log),
        relation_references_to_grant=(pd_view_reference(settings),),
        materialized_views=settings.pd_materialized_views,
        grant_users=settings.pd_grant_select_users,
    )


async def _run_sales_op_module(settings: Settings) -> ModuleRun:
    summary: SalesOpRunSummary = await run_sales_op(settings)
    return ModuleRun(
        code="sales_op",
        schema_name=settings.sales_op_schema,
        has_errors=summary.has_errors,
        has_warnings=summary.has_warnings,
        has_format_changes=False,
        smtp_error_handling_on=settings.sales_op.smtp_error_handling_on,
        smtp_recipients=settings.sales_op.smtp_recipients,
        smtp_subject=settings.sales_op_smtp_error_subject,
        tables_to_grant=summary.loaded_tables,
        relation_references_to_grant=(summary.view_reference,) if summary.view_reference else (),
        materialized_views=settings.sales_op_materialized_views,
        grant_users=settings.sales_op_grant_select_users,
    )


async def _run_post_actions(settings: Settings, module_runs: tuple[ModuleRun, ...]) -> tuple[str, ...]:
    engine = create_postgres_engine(settings)
    failed_modules: list[str] = []
    try:
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
    finally:
        await engine.dispose()

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


async def _send_error_emails(
    settings: Settings,
    *,
    module_runs: tuple[ModuleRun, ...],
    post_action_failed_modules: tuple[str, ...],
    log_path: Path,
    common_post_action_failed: bool,
) -> bool:
    email_failed = False
    post_action_failed_module_set = set(post_action_failed_modules)
    for module_run in module_runs:
        should_send = (
            module_run.has_errors
            or module_run.has_warnings
            or module_run.has_format_changes
            or module_run.code in post_action_failed_module_set
            or common_post_action_failed
        )
        if not should_send or not module_run.smtp_error_handling_on:
            continue
        if not module_run.smtp_recipients:
            LOGGER.error("Не удалось отправить письмо с логом %s: список получателей пуст.", module_run.code)
            email_failed = True
            continue
        email_failed = (
            await _send_log_email(
                settings,
                log_path=log_path,
                recipients=module_run.smtp_recipients,
                subject=module_run.smtp_subject,
            )
            or email_failed
        )

    return email_failed


async def _send_log_email(
    settings: Settings,
    *,
    log_path: Path,
    recipients: tuple[str, ...],
    subject: str,
) -> bool:
    flush_log_handlers()
    full_log_content = await asyncio.to_thread(log_path.read_text, encoding="utf-8")
    log_content = _filter_email_log_content(full_log_content)
    try:
        await send_log_email(
            settings,
            log_content=log_content,
            recipients=recipients,
            subject=subject,
        )
    except SMTPAuthenticationError as error:
        LOGGER.log(
            logging.ERROR,
            "Не удалось отправить письмо с логом получателям %s: ошибка SMTP-аутентификации: %s.",
            ", ".join(recipients),
            error,
        )
        return True
    except Exception:
        LOGGER.exception("Не удалось отправить письмо с логом получателям: %s.", ", ".join(recipients))
        return True

    return False


def _filter_email_log_content(log_content: str) -> str:
    selected_lines: list[str] = []
    for line in log_content.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("level") in {"WARNING", "ERROR", "CRITICAL"}:
            selected_lines.append(line)
    return "\n".join(selected_lines)


if __name__ == "__main__":
    raise SystemExit(main())
