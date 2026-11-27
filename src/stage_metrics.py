"""
Stage-duration histogram for the batch loader.

The loader pushes its metrics to the Pushgateway at the end of a run (see
``metrics.py``). ``el_loader_stage_duration_seconds`` is a Histogram collected
in-process during that run and registered into the same push registry, so it
needs no new environment variable or scrape endpoint.

Labels are bounded by construction: ``module`` is one of ``MODULE_CODES`` and
``stage`` is one of ``STAGES``. File names, paths, dates and sheet names are
deliberately not labels — a stage is a phase of the pipeline, not a source.
"""

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Histogram

from config import MODULE_CODES

if TYPE_CHECKING:
    from collections.abc import Generator

STAGE_DURATION_METRIC = "el_loader_stage_duration_seconds"
STAGE_LABELS = ("module", "stage")
STAGES = ("parse", "parse_sheet", "validate", "load", "commit")
STAGE_MODULES = frozenset(MODULE_CODES)
VALID_STAGES = frozenset(STAGES)
# Batch loads can take minutes; +Inf is added by prometheus_client.
STAGE_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0)

REGISTRY = CollectorRegistry()
STAGE_DURATION_HISTOGRAM = Histogram(
    STAGE_DURATION_METRIC,
    "Длительность стадий загрузки: parse, parse_sheet, validate, load, commit.",
    labelnames=STAGE_LABELS,
    buckets=STAGE_BUCKETS,
    registry=REGISTRY,
)


def observe_stage(module: str, stage: str, duration_seconds: float) -> None:
    """Record one stage duration; reject labels outside the bounded contract."""
    if module not in STAGE_MODULES:
        message = f"Неизвестный модуль метрики стадий: {module!r}."
        raise ValueError(message)
    if stage not in VALID_STAGES:
        message = f"Неизвестная стадия метрики: {stage!r}."
        raise ValueError(message)
    STAGE_DURATION_HISTOGRAM.labels(module=module, stage=stage).observe(duration_seconds)


@contextmanager
def stage_timer(module: str, stage: str) -> Generator[None]:
    """Time a pipeline phase; observation happens even when the phase raises."""
    started_at = time.monotonic()
    try:
        yield
    finally:
        observe_stage(module, stage, time.monotonic() - started_at)
