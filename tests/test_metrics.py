import unittest.mock
from typing import TYPE_CHECKING

from prometheus_client.exposition import generate_latest

import metrics
from metrics import METRICS_GATEWAY_ENV, build_run_metrics, push_run_metrics
from stage_metrics import observe_stage

if TYPE_CHECKING:
    import pytest


def _payload(push_mock: unittest.mock.Mock) -> str:
    return generate_latest(push_mock.call_args.kwargs["registry"]).decode()


def test_build_run_metrics_converts_to_tuples() -> None:
    run_metrics = build_run_metrics(
        "partial_success",
        duration_seconds=7.5,
        module_codes=["quotes", "pd"],
        failed_modules={"pd"},
    )
    assert run_metrics.module_codes == ("quotes", "pd")
    assert run_metrics.failed_modules == ("pd",)
    assert run_metrics.outcome == "partial_success"
    assert run_metrics.duration_seconds == 7.5


def test_push_is_skipped_without_gateway_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(METRICS_GATEWAY_ENV, raising=False)
    with (
        unittest.mock.patch("metrics.push_to_gateway") as put_mock,
        unittest.mock.patch("metrics.pushadd_to_gateway") as post_mock,
    ):
        push_run_metrics(build_run_metrics("success", duration_seconds=1.0, module_codes=[], failed_modules=[]))
    put_mock.assert_not_called()
    post_mock.assert_not_called()


def test_run_snapshot_uses_put_in_its_own_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(METRICS_GATEWAY_ENV, "http://pushgateway:9091")
    monkeypatch.setenv(metrics.METRICS_INSTANCE_ENV, "host-a")
    with (
        unittest.mock.patch("metrics.push_to_gateway") as put_mock,
        unittest.mock.patch("metrics.pushadd_to_gateway"),
    ):
        push_run_metrics(
            build_run_metrics(
                "failure",
                duration_seconds=3.25,
                module_codes=("quotes_nsi", "quotes", "pd", "sales_op"),
                failed_modules=["pd"],
            ),
        )

    put_mock.assert_called_once()
    (url,), kwargs = put_mock.call_args
    assert url == "http://pushgateway:9091"
    assert kwargs["job"] == "excel_loader"
    assert kwargs["grouping_key"] == {"group": "run", "instance": "host-a"}
    payload = _payload(put_mock)
    assert "el_loader_run_status 2" in payload
    assert "el_loader_run_duration_seconds 3.25" in payload
    assert "el_loader_run_timestamp_seconds " in payload
    # Module statuses and the success marker are separate groups, not in PUT.
    assert "el_loader_module_status" not in payload
    assert "el_loader_run_last_success_seconds" not in payload


def test_module_statuses_are_pushed_additively_for_selected_modules_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(METRICS_GATEWAY_ENV, "http://pushgateway:9091")
    monkeypatch.setenv(metrics.METRICS_INSTANCE_ENV, "host-a")
    with (
        unittest.mock.patch("metrics.push_to_gateway"),
        unittest.mock.patch("metrics.pushadd_to_gateway") as post_mock,
    ):
        push_run_metrics(
            build_run_metrics(
                "partial_success",
                duration_seconds=1.0,
                module_codes=("quotes", "pd"),
                failed_modules=["pd"],
            ),
        )

    module_calls = [call for call in post_mock.call_args_list if call.kwargs["grouping_key"]["group"] == "modules"]
    assert len(module_calls) == 1
    assert module_calls[0].kwargs["grouping_key"] == {"group": "modules", "instance": "host-a"}
    payload = generate_latest(module_calls[0].kwargs["registry"]).decode()
    assert 'el_loader_module_status{module="quotes"} 0' in payload
    assert 'el_loader_module_status{module="pd"} 1' in payload
    # Unselected modules are not touched by POST: their last value survives.
    assert "quotes_nsi" not in payload
    assert "sales_op" not in payload


def test_success_marker_is_pushed_additively_and_survives_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(METRICS_GATEWAY_ENV, "http://pushgateway:9091")
    monkeypatch.setenv(metrics.METRICS_INSTANCE_ENV, "host-a")

    def _success_calls() -> list[unittest.mock._Call]:
        with (
            unittest.mock.patch("metrics.push_to_gateway"),
            unittest.mock.patch("metrics.pushadd_to_gateway") as post_mock,
        ):
            push_run_metrics(
                build_run_metrics("success", duration_seconds=1.0, module_codes=[], failed_modules=[]),
            )
        return [call for call in post_mock.call_args_list if call.kwargs["grouping_key"]["group"] == "last_success"]

    success_calls = _success_calls()
    assert len(success_calls) == 1
    assert success_calls[0].kwargs["grouping_key"] == {"group": "last_success", "instance": "host-a"}
    payload = generate_latest(success_calls[0].kwargs["registry"]).decode()
    assert "el_loader_run_last_success_seconds " in payload
    assert "el_loader_run_success_seen 1" in payload

    # A failure leaves the last_success group alone: no push for it at all.
    with (
        unittest.mock.patch("metrics.push_to_gateway"),
        unittest.mock.patch("metrics.pushadd_to_gateway") as post_mock,
    ):
        push_run_metrics(build_run_metrics("failure", duration_seconds=9.0, module_codes=[], failed_modules=[]))
    assert all(call.kwargs["grouping_key"]["group"] != "last_success" for call in post_mock.call_args_list)


def test_push_sends_stage_duration_histogram_in_the_run_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(METRICS_GATEWAY_ENV, "http://pushgateway:9091")
    observe_stage("pd", "validate", 0.42)
    with (
        unittest.mock.patch("metrics.push_to_gateway") as put_mock,
        unittest.mock.patch("metrics.pushadd_to_gateway"),
    ):
        push_run_metrics(build_run_metrics("success", duration_seconds=1.0, module_codes=["pd"], failed_modules=[]))

    payload = _payload(put_mock)
    assert "# TYPE el_loader_stage_duration_seconds histogram" in payload
    bucket_line = next(
        line
        for line in payload.splitlines()
        if line.startswith('el_loader_stage_duration_seconds_bucket{le="0.5",module="pd",stage="validate"}')
    )
    assert float(bucket_line.rsplit(" ", 1)[1]) >= 1
    assert 'el_loader_stage_duration_seconds_count{module="pd",stage="validate"}' in payload


def test_push_failures_are_contained_for_each_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(METRICS_GATEWAY_ENV, "http://pushgateway:9091")
    with (
        unittest.mock.patch("metrics.push_to_gateway", side_effect=OSError("connection refused")) as put_mock,
        unittest.mock.patch("metrics.pushadd_to_gateway", side_effect=OSError("connection refused")) as post_mock,
    ):
        # Отказ доставки не должен прерывать запуск: логируем и продолжаем.
        push_run_metrics(build_run_metrics("failure", duration_seconds=9.0, module_codes=[], failed_modules=[]))
    put_mock.assert_called_once()
    assert post_mock.call_count == 1  # only the module group on a failure
