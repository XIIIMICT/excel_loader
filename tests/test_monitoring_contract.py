"""
Static checks for the shared monitoring contract.

The drift script lives in the workspace-level `infra/` (not in this repo). It
is exercised when the sibling stacks are checked out next to this one; the
naming/label checks below always run.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import metrics
import stage_metrics
from config import MODULE_CODES

PROJECT_DIR = Path(__file__).resolve().parents[1]
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count", "_created")


def _declared_metrics() -> set[str]:
    return {
        value
        for module in (metrics, stage_metrics)
        for name, value in vars(module).items()
        if name.endswith("_METRIC") and isinstance(value, str)
    }


def _base_metric(token: str) -> str:
    for suffix in HISTOGRAM_SUFFIXES:
        if token.endswith(suffix):
            return token.removesuffix(suffix)
    return token


def test_metric_constants_follow_the_el_loader_prefix() -> None:
    declared = _declared_metrics()
    assert declared
    for name in declared:
        assert re.fullmatch(r"el_loader_[a-z][a-z0-9_]*", name), name
    assert stage_metrics.STAGE_DURATION_METRIC == "el_loader_stage_duration_seconds"


def test_stage_histogram_labels_are_bounded() -> None:
    assert stage_metrics.STAGE_LABELS == ("module", "stage")
    assert set(stage_metrics.STAGES) == {"parse", "parse_sheet", "validate", "load", "commit"}
    assert frozenset(MODULE_CODES) == stage_metrics.STAGE_MODULES
    assert len(stage_metrics.STAGE_BUCKETS) == len(set(stage_metrics.STAGE_BUCKETS))
    assert stage_metrics.STAGE_BUCKETS[-1] == 300.0


def test_dashboard_and_alerts_reference_only_declared_metrics() -> None:
    dashboard = (PROJECT_DIR / "infra/grafana/provisioning/dashboards-json/overview.json").read_text(encoding="utf-8")
    alerts = (PROJECT_DIR / "infra/prometheus/alert-rules.yml").read_text(encoding="utf-8")
    declared = _declared_metrics()
    for text in (dashboard, alerts):
        referenced = {_base_metric(token) for token in re.findall(r"\bel_loader_[a-z0-9_]+", text)}
        assert referenced <= declared, referenced - declared
    assert all(name.startswith("ExcelLoader") for name in re.findall(r"^\s*- alert: (\S+)", alerts, re.MULTILINE))


def test_dashboard_has_the_compact_stage_panels() -> None:
    dashboard = json.loads(
        (PROJECT_DIR / "infra/grafana/provisioning/dashboards-json/overview.json").read_text(encoding="utf-8"),
    )
    stage_panels = [
        panel
        for panel in dashboard["panels"]
        if any("el_loader_stage_duration_seconds" in target["expr"] for target in panel["targets"])
    ]
    assert len(stage_panels) >= 2
    assert any("histogram_quantile" in target["expr"] for panel in stage_panels for target in panel["targets"])


def test_shared_monitoring_files_have_no_drift() -> None:
    workspace = PROJECT_DIR.parent
    script = workspace / "infra" / "check-drift.py"
    siblings = (workspace / "infra", workspace / "ReportCollector", workspace / "DocMasker", PROJECT_DIR)
    if not script.is_file() or not all(path.is_dir() for path in siblings):
        pytest.skip("cross-stack drift check needs the sibling stacks checked out in the workspace")
    result = subprocess.run(  # noqa: S603  # fixed workspace script, no user input
        [sys.executable, str(script)],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Дрейфа нет." in result.stdout
