import unittest.mock
from typing import TYPE_CHECKING

from prometheus_client.exposition import generate_latest

from metrics import METRICS_GATEWAY_ENV, build_run_metrics, push_run_metrics

if TYPE_CHECKING:
    import pytest


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
    with unittest.mock.patch("metrics.push_to_gateway") as push_mock:
        push_run_metrics(build_run_metrics("success", duration_seconds=1.0, module_codes=[], failed_modules=[]))
    push_mock.assert_not_called()


def test_push_sends_full_run_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(METRICS_GATEWAY_ENV, "http://pushgateway:9091")
    with unittest.mock.patch("metrics.push_to_gateway") as push_mock:
        push_run_metrics(
            build_run_metrics(
                "success",
                duration_seconds=3.25,
                module_codes=("quotes_nsi", "quotes", "pd", "sales_op"),
                failed_modules=["pd"],
            ),
        )
    push_mock.assert_called_once()
    (url,), kwargs = push_mock.call_args
    assert url == "http://pushgateway:9091"
    assert kwargs["job"] == "excel_loader"

    payload = generate_latest(kwargs["registry"]).decode()
    assert "el_loader_run_status 0" in payload
    assert "el_loader_run_duration_seconds 3.25" in payload
    assert "el_loader_run_last_success_seconds " in payload
    assert 'el_loader_module_status{module="quotes_nsi"} 0' in payload
    assert 'el_loader_module_status{module="quotes"} 0' in payload
    assert 'el_loader_module_status{module="pd"} 1' in payload
    assert 'el_loader_module_status{module="sales_op"} 0' in payload


def test_push_failure_state_has_no_last_success_and_module_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(METRICS_GATEWAY_ENV, "http://pushgateway:9091")
    with unittest.mock.patch("metrics.push_to_gateway") as push_mock:
        push_run_metrics(
            build_run_metrics("failure", duration_seconds=9.0, module_codes=["pd"], failed_modules=["pd"]),
        )
    payload = generate_latest(push_mock.call_args.kwargs["registry"]).decode()
    assert "el_loader_run_status 2" in payload
    assert "el_loader_run_last_success_seconds" not in payload
    assert 'el_loader_module_status{module="pd"} 1' in payload


def test_push_failure_delivery_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(METRICS_GATEWAY_ENV, "http://pushgateway:9091")
    with unittest.mock.patch("metrics.push_to_gateway", side_effect=OSError("connection refused")):
        # Отказ доставки не должен прерывать запуск: логируем и продолжаем.
        push_run_metrics(
            build_run_metrics("failure", duration_seconds=9.0, module_codes=[], failed_modules=[]),
        )
