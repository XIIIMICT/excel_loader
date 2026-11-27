import re

import pytest
from prometheus_client.exposition import generate_latest

from stage_metrics import (
    REGISTRY,
    STAGE_DURATION_METRIC,
    STAGE_LABELS,
    STAGES,
    observe_stage,
    stage_timer,
)


def _count(payload: str, module: str, stage: str) -> int:
    prefix = re.escape(f'{STAGE_DURATION_METRIC}_count{{module="{module}",stage="{stage}"}} ')
    match = re.search(rf"^{prefix}(\d+(?:\.\d+)?)$", payload, re.MULTILINE)
    return int(float(match.group(1))) if match else 0


def test_histogram_is_registered_with_the_documented_name_and_labels() -> None:
    payload = generate_latest(REGISTRY).decode()
    assert f"# TYPE {STAGE_DURATION_METRIC} histogram" in payload
    assert f"# HELP {STAGE_DURATION_METRIC} " in payload
    assert STAGE_LABELS == ("module", "stage")


def test_stage_timer_observes_one_sample_with_expected_labels() -> None:
    before = _count(generate_latest(REGISTRY).decode(), "pd", "parse")
    with stage_timer("pd", "parse"):
        pass
    payload = generate_latest(REGISTRY).decode()
    assert _count(payload, "pd", "parse") == before + 1
    assert f'{STAGE_DURATION_METRIC}_count{{module="pd",stage="parse"}}' in payload


def test_timer_observes_even_when_the_stage_raises() -> None:
    before = _count(generate_latest(REGISTRY).decode(), "sales_op", "parse_sheet")
    message = "boom"
    with pytest.raises(RuntimeError, match=message), stage_timer("sales_op", "parse_sheet"):
        raise RuntimeError(message)
    assert _count(generate_latest(REGISTRY).decode(), "sales_op", "parse_sheet") == before + 1


def test_every_declared_stage_can_be_observed() -> None:
    for stage in STAGES:
        observe_stage("quotes", stage, 0.25)
    payload = generate_latest(REGISTRY).decode()
    for stage in STAGES:
        assert _count(payload, "quotes", stage) >= 1
        assert f'{STAGE_DURATION_METRIC}_bucket{{le="0.5",module="quotes",stage="{stage}"}}' in payload


def test_unknown_labels_are_rejected() -> None:
    with pytest.raises(ValueError, match="Неизвестная стадия"):
        observe_stage("pd", "unknown", 1.0)
    with pytest.raises(ValueError, match="Неизвестный модуль"):
        observe_stage("/data/pd/pd.xlsx", "parse", 1.0)


def test_histogram_has_no_high_cardinality_labels() -> None:
    observe_stage("quotes_nsi", "commit", 0.1)
    payload = generate_latest(REGISTRY).decode()
    line = next(line for line in payload.splitlines() if line.startswith(f"{STAGE_DURATION_METRIC}_count{{"))
    labels = re.findall(r"(\w+)=", line[line.index("{") + 1 :])
    assert set(labels) == set(STAGE_LABELS)
