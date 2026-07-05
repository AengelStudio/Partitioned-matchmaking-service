from fastapi.testclient import TestClient

from app.callback_dispatcher.http import create_app
from app.callback_dispatcher.metrics import CallbackDispatcherMetrics


def test_callback_dispatcher_metrics_format_prometheus() -> None:
    metrics = CallbackDispatcherMetrics()
    metrics.record_claimed(2)
    metrics.record_delivered(12.5)
    metrics.record_retry()
    metrics.record_failed()
    metrics.record_loop(7.25)

    output = metrics.format_prometheus("dispatcher-1")

    assert 'dispatcher_id="dispatcher-1"' in output
    assert "pms_callback_events_claimed_total" in output
    assert "pms_callback_delivered_total" in output
    assert "pms_callback_failed_total" in output
    assert "pms_callback_retries_total" in output
    assert "pms_callback_delivery_latency_ms" in output
    assert "pms_callback_loops_completed_total" in output


def test_callback_dispatcher_health_and_metrics_endpoints() -> None:
    metrics = CallbackDispatcherMetrics()
    app = create_app(metrics, "dispatcher-1", metrics_enabled=True)
    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "callback-dispatcher",
        "dispatcher_id": "dispatcher-1",
    }

    response = client.get("/metrics")
    assert response.status_code == 200
    assert "pms_callback_dispatcher_info" in response.text
