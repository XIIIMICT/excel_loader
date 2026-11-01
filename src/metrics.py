"""
Push-run metrics for the Excel Loader monitoring stack.

The loader is a batch CLI: it starts, runs and exits, so Prometheus cannot
scrape a `/metrics` endpoint from it. Instead the process pushes the run
state to a Prometheus Pushgateway when one is configured through the
``METRICS_PUSHGATEWAY_URL`` environment variable. Each run replaces the
previous series (one grouping key per host), and Prometheus keeps the run
history for the dashboards.

Pushed series (all gauges):

- ``el_loader_run_status`` — 0 success, 1 partial_success, 2 failure;
- ``el_loader_run_duration_seconds`` — wall duration of the whole run;
- ``el_loader_run_timestamp_seconds`` — Unix time of the push;
- ``el_loader_run_last_success_seconds`` — Unix time of the last fully
  successful run. Pushed only on success: the Pushgateway keeps the
  previous value, so alerts in ``infra/prometheus/alert-rules.yml`` can
  fire on an absence of successful runs.
- ``el_loader_module_status{module}`` — 1 when the module failed, 0 when it
  succeeded. Only selected modules are pushed, so stale statuses of
  unselected modules stay untouched at the Pushgateway.
"""

import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

from app_logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

METRICS_GATEWAY_ENV = "METRICS_PUSHGATEWAY_URL"
METRICS_JOB_NAME = "excel_loader"
METRICS_PUSH_TIMEOUT_SECONDS = 10

OUTCOME_STATUS_VALUES = {"success": 0, "partial_success": 1, "failure": 2}

RUN_STATUS_METRIC = "el_loader_run_status"
RUN_DURATION_METRIC = "el_loader_run_duration_seconds"
RUN_TIMESTAMP_METRIC = "el_loader_run_timestamp_seconds"
RUN_LAST_SUCCESS_METRIC = "el_loader_run_last_success_seconds"
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


def push_run_metrics(run_metrics: RunMetrics) -> None:
    """Push the run metrics when a Pushgateway is configured; never raise."""
    gateway_url = os.environ.get(METRICS_GATEWAY_ENV, "").strip()
    if not gateway_url:
        return

    registry = CollectorRegistry()

    Gauge(RUN_STATUS_METRIC, "Итог последнего запуска: 0 success, 1 partial_success, 2 failure", registry=registry).set(
        OUTCOME_STATUS_VALUES[run_metrics.outcome],
    )
    Gauge(RUN_DURATION_METRIC, "Длительность последнего запуска в секундах", registry=registry).set(
        run_metrics.duration_seconds,
    )
    Gauge(RUN_TIMESTAMP_METRIC, "Unix-время последнего запуска", registry=registry).set(time.time())

    if run_metrics.outcome == "success":
        Gauge(
            RUN_LAST_SUCCESS_METRIC,
            "Unix-время последнего полностью успешного запуска",
            registry=registry,
        ).set(time.time())

    module_gauge = Gauge(
        MODULE_STATUS_METRIC,
        "Статус модуля последнего запуска: 1 ошибка, 0 успех",
        ["module"],
        registry=registry,
    )
    for module_code in run_metrics.module_codes:
        module_gauge.labels(module=module_code).set(1 if module_code in run_metrics.failed_modules else 0)

    try:
        push_to_gateway(
            gateway_url,
            job=METRICS_JOB_NAME,
            registry=registry,
            timeout=METRICS_PUSH_TIMEOUT_SECONDS,
        )
    except Exception:
        # The monitoring stack is unreachable: its Alertmanager will not be
        # able to deliver the e-mail alert. Keep it in the run log file.
        LOGGER.exception(
            "Не удалось отправить метрики запуска в %s — стек мониторинга недоступен, "
            "почтовый алерт через Alertmanager отправлен не будет.",
            gateway_url,
        )
        return

    LOGGER.info("Метрики запуска отправлены в %s.", gateway_url)
