from app.worker.metrics import StageTimer, WorkerMetrics


def test_stage_timer_accumulates() -> None:
    timer = StageTimer()
    timer.record(10.0)
    timer.record(20.0)
    assert timer.last_ms == 20.0
    assert timer.total_ms == 30.0
    assert timer.count == 2


def test_record_loop_stats_tracks_backlog_signals() -> None:
    metrics = WorkerMetrics()
    metrics.record_loop_stats(
        tickets_fetched=80,
        pairs_found=40,
        matches_created=5,
        pairs_skipped=35,
        max_ticket_wait_s=120.0,
        avg_ticket_wait_s=60.0,
        budget_exceeded=True,
    )
    assert metrics.tickets_fetched_last_loop == 80
    assert metrics.pairs_skipped_total == 35
    assert metrics.loop_budget_exceeded_total == 1
    assert metrics.max_ticket_wait_seconds == 120.0


def test_format_prometheus_includes_new_metrics() -> None:
    metrics = WorkerMetrics()
    metrics.record_match()
    metrics.record_leases_renewed(3)
    metrics.lease_ops_ms.record(12.5)
    metrics.record_jittered_sleep(437.0)
    text = metrics.format_prometheus("worker-test")
    required = [
        "pms_worker_matches_created_total",
        "pms_worker_leases_renewed_total",
        "pms_worker_jittered_sleep_ms",
        "pms_worker_lease_ops_ms_sum",
        "pms_worker_loop_budget_exceeded_total",
        "pms_worker_max_ticket_wait_seconds",
    ]
    for name in required:
        assert name in text
