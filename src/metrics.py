"""
Push-run metrics for the Excel Loader monitoring stack.

The loader is a batch CLI: it starts, runs and exits, so Prometheus cannot
scrape a ``/metrics`` endpoint from it. Instead the process pushes the run
state to a Prometheus Pushgateway when one is configured through the
``METRICS_PUSHGATEWAY_URL`` environment variable.

A Pushgateway group is the unit of replacement. ``push_to_gateway`` issues
HTTP PUT and replaces the whole group; ``pushadd_to_gateway`` issues POST and
updates only the series it carries. The three families therefore use separate
groups (``group`` label in the grouping key):

- ``run`` (PUT, every run): ``el_loader_run_status``, ``..._duration_seconds``,
  ``..._timestamp_seconds`` and the stage histogram. This is the snapshot of
  the last run; replacing the whole group removes stale stage buckets.
- ``modules`` (POST, every run): ``el_loader_module_status{module}`` — only the
  selected modules are updated, statuses of the others keep their last value.
- ``last_success`` (POST): ``el_loader_run_last_success_seconds`` and
  ``el_loader_run_success_seen``. Pushed only on full success, so a failed run
  leaves the previous success marker intact. ``success_seen`` is the explicitly
  checkable "there was at least one success" flag: the alert also fires when no
  success has ever been recorded (its series is then absent).

Pushed series (all gauges unless noted):

- ``el_loader_run_status`` — 0 success, 1 partial_success, 2 failure;
- ``el_loader_run_duration_seconds`` — wall duration of the whole run;
- ``el_loader_run_timestamp_seconds`` — Unix time of the push;
- ``el_loader_run_last_success_seconds`` — Unix time of the last fully
  successful run (only on success, separate group);
- ``el_loader_run_success_seen`` — 1 once a full success has been pushed;
- ``el_loader_module_status{module}`` — 1 when the module failed, 0 when it
  succeeded. Only selected modules are pushed, so stale statuses of
  unselected modules stay untouched at the Pushgateway.
- ``el_loader_stage_duration_seconds{module,stage}`` — histogram (one push),
  buckets are a snapshot of the last run; never ``rate()`` them.

``METRICS_INSTANCE`` (optional env, defaults to the host name) becomes the
``instance`` grouping label: concurrent runs and hosts with different instance
values keep separate groups, so one run cannot erase another's state.
"""

import os
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Gauge, push_to_gateway, pushadd_to_gateway

from app_logging import get_logger
from stage_metrics import STAGE_DURATION_HISTOGRAM

if TYPE_CHECKING:
    from collections.abc import Iterable

METRICS_GATEWAY_ENV = "METRICS_PUSHGATEWAY_URL"
METRICS_INSTANCE_ENV = "METRICS_INSTANCE"
METRICS_JOB_NAME = "excel_loader"
METRICS_PUSH_TIMEOUT_SECONDS = 10

GROUP_RUN = "run"
GROUP_MODULES = "modules"
GROUP_LAST_SUCCESS = "last_success"

OUTCOME_STATUS_VALUES = {"success": 0, "partial_success": 1, "failure": 2}

RUN_STATUS_METRIC = "el_loader_run_status"
RUN_DURATION_METRIC = "el_loader_run_duration_seconds"
RUN_TIMESTAMP_METRIC = "el_loader_run_timestamp_seconds"
RUN_LAST_SUCCESS_METRIC = "el_loader_run_last_success_seconds"
RUN_SUCCESS_SEEN_METRIC = "el_loader_run_success_seen"
MODULE_STATUS_METRIC = "el_loader_module_status"

LOGGER = get_logger("metrics")


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """Run state to push to the Pushgateway."""

    outcome: str
    duration_seconds: float
    module_codes: tuple[str, ...]
    failed_modules: tuple[str, ...]


def build_run_metrics(
    outcome: str,
    *,
    duration_seconds: float,
    module_codes: Iterable[str],
    failed_modules: Iterable[str],
) -> RunMetrics:
    """Build a RunMetrics snapshot from the module-run outcomes."""
    return RunMetrics(
        outcome=outcome,
        duration_seconds=duration_seconds,
        module_codes=tuple(module_codes),
        failed_modules=tuple(failed_modules),
    )


def _grouping_key(group: str, instance: str) -> dict[str, str]:
    return {"group": group, "instance": instance}


def _push(
    gateway_url: str,
    *,
    job: str,
    registry: CollectorRegistry,
    grouping_key: dict[str, str],
    additive: bool,
) -> bool:
    """Push one group; log delivery errors and never interrupt the run."""
    try:
        if additive:
            pushadd_to_gateway(
                gateway_url,
                job=job,
                registry=registry,
                grouping_key=grouping_key,
                timeout=METRICS_PUSH_TIMEOUT_SECONDS,
            )
        else:
            push_to_gateway(
                gateway_url,
                job=job,
                registry=registry,
                grouping_key=grouping_key,
                timeout=METRICS_PUSH_TIMEOUT_SECONDS,
            )
    except Exception:
        # The monitoring stack is unreachable: its Alertmanager will not be
        # able to deliver the e-mail alert. Keep it in the run log file.
        LOGGER.exception(
            "Не удалось отправить метрики запуска (группа %s) в %s — стек мониторинга недоступен, "
            "почтовый алерт через Alertmanager отправлен не будет.",
            grouping_key["group"],
            gateway_url,
        )
        return False
    return True


def push_run_metrics(run_metrics: RunMetrics) -> None:
    """Push the run metrics when a Pushgateway is configured; never raise."""
    gateway_url = os.environ.get(METRICS_GATEWAY_ENV, "").strip()
    if not gateway_url:
        return

    instance = os.environ.get(METRICS_INSTANCE_ENV, "").strip() or socket.gethostname()
    pushed_at = time.time()

    # PUT group: the whole run snapshot is replaced once per run, which also
    # removes buckets of stages that did not run this time.
    run_registry = CollectorRegistry()
    Gauge(
        RUN_STATUS_METRIC,
        "Итог последнего запуска: 0 success, 1 partial_success, 2 failure",
        registry=run_registry,
    ).set(OUTCOME_STATUS_VALUES[run_metrics.outcome])
    Gauge(RUN_DURATION_METRIC, "Длительность последнего запуска в секундах", registry=run_registry).set(
        run_metrics.duration_seconds,
    )
    Gauge(RUN_TIMESTAMP_METRIC, "Unix-время последнего запуска", registry=run_registry).set(pushed_at)
    run_registry.register(STAGE_DURATION_HISTOGRAM)

    # POST group: update only the modules that ran; the others keep their last
    # value in the Pushgateway (and so do other hosts in their own groups).
    module_registry = CollectorRegistry()
    module_gauge = Gauge(
        MODULE_STATUS_METRIC,
        "Статус модуля последнего запуска: 1 ошибка, 0 успех",
        ["module"],
        registry=module_registry,
    )
    for module_code in run_metrics.module_codes:
        module_gauge.labels(module=module_code).set(1 if module_code in run_metrics.failed_modules else 0)

    succeeded = _push(
        gateway_url,
        job=METRICS_JOB_NAME,
        registry=run_registry,
        grouping_key=_grouping_key(GROUP_RUN, instance),
        additive=False,
    )
    succeeded &= _push(
        gateway_url,
        job=METRICS_JOB_NAME,
        registry=module_registry,
        grouping_key=_grouping_key(GROUP_MODULES, instance),
        additive=True,
    )

    if run_metrics.outcome == "success":
        # POST group: a failed run never touches it, so the last success marker
        # survives. success_seen makes "no success ever" explicitly checkable.
        success_registry = CollectorRegistry()
        Gauge(
            RUN_LAST_SUCCESS_METRIC,
            "Unix-время последнего полностью успешного запуска",
            registry=success_registry,
        ).set(pushed_at)
        Gauge(
            RUN_SUCCESS_SEEN_METRIC,
            "Был ли хотя бы один полностью успешный запуск: 1 да",
            registry=success_registry,
        ).set(1)
        success_pushed = _push(
            gateway_url,
            job=METRICS_JOB_NAME,
            registry=success_registry,
            grouping_key=_grouping_key(GROUP_LAST_SUCCESS, instance),
            additive=True,
        )
        succeeded = success_pushed and succeeded

    if succeeded:
        LOGGER.info("Метрики запуска отправлены в %s.", gateway_url)
